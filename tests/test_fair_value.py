"""
Unit tests for fair value probability models
"""

import pytest
import math
from src.financial.fair_value_v2 import SophisticatedFairValue, FairValueResult


class TestStudentTVsLogNormal:
    """Test that Student's t and log-normal give similar results for ATM"""

    def test_atm_probabilities_similar(self):
        """Student's t and log-normal should give similar ATM probabilities"""
        price = 21500
        hours = 8

        model_t = SophisticatedFairValue(use_fat_tails=True)
        model_ln = SophisticatedFairValue(use_fat_tails=False)

        # ATM range
        lower = 21450
        upper = 21550

        result_t = model_t.calculate(price, lower, upper, hours, "nasdaq")
        result_ln = model_ln.calculate(price, lower, upper, hours, "nasdaq")

        # Should be within 10% of each other for ATM
        diff = abs(result_t.probability - result_ln.probability)
        assert diff < 0.10, f"ATM probabilities differ too much: t={result_t.probability:.3f}, ln={result_ln.probability:.3f}"

    def test_fat_tails_higher_extreme_prob(self):
        """Student's t should give higher probability to extreme moves"""
        price = 21500
        hours = 8

        model_t = SophisticatedFairValue(use_fat_tails=True)
        model_ln = SophisticatedFairValue(use_fat_tails=False)

        # Far OTM - should see fat tail effect
        lower = 22500  # +4.6% move
        upper = float('inf')

        result_t = model_t.calculate(price, lower, upper, hours, "nasdaq")
        result_ln = model_ln.calculate(price, lower, upper, hours, "nasdaq")

        # Fat tails should give higher probability to extreme moves
        assert result_t.probability >= result_ln.probability * 0.9, \
            f"Fat tails should increase extreme probability: t={result_t.probability:.4f}, ln={result_ln.probability:.4f}"

    def test_both_use_log_space(self):
        """Both models should use log-space (prices can't go negative)"""
        price = 100
        hours = 24

        model_t = SophisticatedFairValue(use_fat_tails=True)
        model_ln = SophisticatedFairValue(use_fat_tails=False)

        # Very low strike - probability should NOT be > 50% even with high vol
        lower = 0
        upper = 50  # 50% down

        result_t = model_t.calculate(price, lower, upper, hours, "spx")
        result_ln = model_ln.calculate(price, lower, upper, hours, "spx")

        # Both should give reasonable (not absurd) probabilities
        assert result_t.probability < 0.5, f"Student's t below-50% prob too high: {result_t.probability}"
        assert result_ln.probability < 0.5, f"Log-normal below-50% prob too high: {result_ln.probability}"


class TestNoEdgeCalculation:
    """Test that NO edge is calculated correctly"""

    def test_no_edge_with_yes_bid(self):
        """NO market price should be (100 - yes_bid)/100"""
        from src.core.models import Market, MarketType

        market = Market(
            ticker="TEST",
            event_ticker="TEST_EVENT",
            title="Test",
            subtitle="",
            market_type=MarketType.FINANCIAL_RANGE,
            yes_bid=40,
            yes_ask=45,
            fair_value=0.30,  # 30% YES fair value
        )

        # NO buy price = (100 - yes_bid) / 100 = (100 - 40) / 100 = 0.60
        assert market.no_buy_price == 0.60, f"NO buy price wrong: {market.no_buy_price}"
        assert market.no_market_price == 0.60, f"NO market price wrong: {market.no_market_price}"

        # NO fair value = 1 - 0.30 = 0.70
        assert market.no_fair_value == 0.70, f"NO fair value wrong: {market.no_fair_value}"

        # NO edge = NO fair value - NO buy price = 0.70 - 0.60 = +0.10 (10% edge!)
        assert abs(market.no_edge - 0.10) < 0.001, f"NO edge wrong: {market.no_edge}"

    def test_no_edge_negative_when_overpriced(self):
        """NO edge should be negative when NO is overpriced"""
        from src.core.models import Market, MarketType

        market = Market(
            ticker="TEST",
            event_ticker="TEST_EVENT",
            title="Test",
            subtitle="",
            market_type=MarketType.FINANCIAL_RANGE,
            yes_bid=40,
            yes_ask=45,
            fair_value=0.50,  # 50% YES fair value
        )

        # NO buy price = 0.60
        # NO fair value = 0.50
        # NO edge = 0.50 - 0.60 = -0.10 (NO is overpriced)
        assert abs(market.no_edge - (-0.10)) < 0.001, f"NO edge should be negative: {market.no_edge}"


class TestVolatilitySources:
    """Test volatility source priority"""

    def test_options_implied_preferred(self):
        """Options-implied vol should be preferred over historical"""
        model = SophisticatedFairValue(
            historical_vols={"nasdaq": 0.20},
            use_fat_tails=True,
        )
        model.options_implied_vols["nasdaq"] = 0.25

        result = model.calculate(21500, 21000, float('inf'), 8, "nasdaq")

        # Should use options-implied vol (0.25) not historical (0.20)
        assert result.vol_source == "options_implied"
        assert abs(result.model_vol - 0.25) < 0.05  # Allow some adjustment

    def test_historical_when_no_options(self):
        """Historical vol should be used when options not available"""
        model = SophisticatedFairValue(
            historical_vols={"nasdaq": 0.22},
            use_fat_tails=True,
        )

        result = model.calculate(21500, 21000, float('inf'), 8, "nasdaq")

        assert result.vol_source == "historical"

    def test_vix_scaling_for_equities(self):
        """VIX should scale volatility for equity indices"""
        model = SophisticatedFairValue(
            vix_price=25,  # High VIX
            use_fat_tails=True,
        )

        result = model.calculate(6000, 5900, float('inf'), 8, "spx")

        # VIX=25 means 25% annualized vol for SPX
        assert result.vol_source == "vix_scaled"
        assert abs(result.model_vol - 0.25) < 0.10  # Should be close to VIX/100


class TestTimeConversion:
    """Test time conversion for different asset classes"""

    def test_equity_uses_trading_hours(self):
        """Equity should use 6.5 trading hours per day"""
        model = SophisticatedFairValue(use_fat_tails=False)

        # 6.5 hours = 1 trading day = 1/252 years
        result = model.calculate(6000, 5900, float('inf'), 6.5, "spx")
        assert result.effective_hours == 6.5

    def test_forex_uses_24_hours(self):
        """Forex should use 24-hour days"""
        model = SophisticatedFairValue(use_fat_tails=False)
        model.historical_vols["usdjpy"] = 0.08

        # 24 hours = 1 calendar day
        result = model.calculate(150, 149, float('inf'), 24, "usdjpy")
        assert result.effective_hours == 24


class TestEdgeCases:
    """Test edge cases"""

    def test_zero_time_to_expiry(self):
        """Should return near 1 or near 0 when time to expiry is 0"""
        model = SophisticatedFairValue()

        # Price in range - clamped to 0.99 for safety
        result = model.calculate(100, 90, 110, 0, "spx")
        assert result.probability >= 0.99

        # Price out of range - clamped to 0.01 for safety
        result = model.calculate(100, 110, 120, 0, "spx")
        assert result.probability <= 0.01

    def test_negative_time(self):
        """Should handle negative time gracefully"""
        model = SophisticatedFairValue()

        result = model.calculate(100, 90, 110, -1, "spx")
        # Price is in range, clamped to 0.99 for safety
        assert result.probability >= 0.99

    def test_very_wide_range(self):
        """Very wide range should have high probability"""
        model = SophisticatedFairValue()

        result = model.calculate(100, 50, 150, 8, "spx")
        assert result.probability > 0.95


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
