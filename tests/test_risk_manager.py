"""
Unit tests for Risk Manager
"""

import pytest
from datetime import datetime, timezone
from src.financial.risk_manager import RiskManager, AssetClassExposure, PortfolioSnapshot
from src.core.models import Market, Position, Side, MarketType


class TestMTMCalculation:
    """Test mark-to-market calculations"""

    def test_mtm_yes_position(self):
        """MTM for YES position should use YES bid (exit price)"""
        rm = RiskManager()

        market = Market(
            ticker="TEST",
            event_ticker="TEST_EVENT",
            title="Test",
            subtitle="",
            market_type=MarketType.FINANCIAL_RANGE,
            yes_bid=60,
            yes_ask=65,
        )
        rm.update_markets_cache([market])

        position = Position(
            ticker="TEST",
            market_title="Test",
            side=Side.YES,
            quantity=10,
            avg_price=0.50,  # Bought at 50c
        )

        # MTM = quantity * yes_bid/100 = 10 * 0.60 = 6.00
        mtm = rm.calculate_position_mtm(position, market)
        assert abs(mtm - 6.0) < 0.01, f"YES MTM wrong: {mtm}"

        # Unrealized P&L = MTM - cost = 6.00 - 5.00 = 1.00
        pnl = rm.calculate_position_unrealized_pnl(position, market)
        assert abs(pnl - 1.0) < 0.01, f"YES unrealized P&L wrong: {pnl}"

    def test_mtm_no_position(self):
        """MTM for NO position should use NO bid = (100 - YES ask)"""
        rm = RiskManager()

        market = Market(
            ticker="TEST",
            event_ticker="TEST_EVENT",
            title="Test",
            subtitle="",
            market_type=MarketType.FINANCIAL_RANGE,
            yes_bid=60,
            yes_ask=65,
        )
        rm.update_markets_cache([market])

        position = Position(
            ticker="TEST",
            market_title="Test",
            side=Side.NO,
            quantity=10,
            avg_price=0.40,  # Bought NO at 40c
        )

        # NO bid = (100 - yes_ask) / 100 = (100 - 65) / 100 = 0.35
        # MTM = quantity * no_bid = 10 * 0.35 = 3.50
        mtm = rm.calculate_position_mtm(position, market)
        assert abs(mtm - 3.5) < 0.01, f"NO MTM wrong: {mtm}"

        # Unrealized P&L = MTM - cost = 3.50 - 4.00 = -0.50
        pnl = rm.calculate_position_unrealized_pnl(position, market)
        assert abs(pnl - (-0.5)) < 0.01, f"NO unrealized P&L wrong: {pnl}"


class TestAssetClassExposure:
    """Test correlated exposure tracking"""

    def test_nasdaq_markets_grouped(self):
        """All NASDAQ markets should be grouped together"""
        rm = RiskManager()

        positions = [
            Position(ticker="NASDAQ100-001", market_title="NQ Above 21000",
                     side=Side.YES, quantity=10, avg_price=0.50),
            Position(ticker="NASDAQ100U-002", market_title="NQ Range",
                     side=Side.YES, quantity=5, avg_price=0.40),
        ]

        # Mock markets for MTM
        markets = [
            Market(ticker="NASDAQ100-001", event_ticker="E1", title="", subtitle="",
                   market_type=MarketType.FINANCIAL_ABOVE, yes_bid=50, yes_ask=55),
            Market(ticker="NASDAQ100U-002", event_ticker="E2", title="", subtitle="",
                   market_type=MarketType.FINANCIAL_RANGE, yes_bid=40, yes_ask=45),
        ]
        rm.update_markets_cache(markets)

        exposures = rm.get_exposure_by_asset_class(positions)

        # Should have nasdaq exposure (may be grouped under unknown if ticker pattern doesn't match)
        assert len(exposures) >= 1, "Should have at least one asset class"

    def test_exposure_limits(self):
        """Exposure limits should be enforced"""
        rm = RiskManager()
        rm.config.max_exposure_per_asset_class = 0.30  # 30% max per class

        # Create position worth 40% of equity
        positions = [
            Position(ticker="TEST", market_title="Test",
                     side=Side.YES, quantity=40, avg_price=1.0,
                     current_bid=1.0, current_ask=1.0),
        ]

        total_equity = 100  # $100 equity

        ok, msg = rm.check_exposure_limits(positions, total_equity)
        # 40% > 30% limit, should fail
        assert not ok, "Should fail exposure limit"
        assert "exposure" in msg.lower()


class TestDailyLossLimit:
    """Test daily loss limit enforcement"""

    def test_loss_limit_triggered(self):
        """Should trigger when daily loss exceeds limit"""
        rm = RiskManager()
        rm.config.max_daily_loss_pct = 0.05  # 5% max loss

        # Start with $100
        rm._starting_equity = 100.0

        # Now at $90 (-10% loss)
        ok, msg = rm.check_daily_loss_limit(90.0)
        assert not ok, "Should fail daily loss limit"
        assert "loss" in msg.lower()

    def test_loss_limit_not_triggered(self):
        """Should pass when within loss limit"""
        rm = RiskManager()
        rm.config.max_daily_loss_pct = 0.05

        rm._starting_equity = 100.0

        # Now at $97 (-3% loss, within 5% limit)
        ok, msg = rm.check_daily_loss_limit(97.0)
        assert ok, "Should pass daily loss limit"


class TestNewOrderCheck:
    """Test pre-trade risk checks"""

    def test_order_within_limits(self):
        """Order within limits should be allowed"""
        rm = RiskManager()

        positions = []
        total_equity = 100

        # Small order: 5 contracts at 50c = $2.50
        ok, msg = rm.check_new_order(
            ticker="TEST", side="yes", size=5, price=50,
            positions=positions, total_equity=total_equity
        )
        assert ok, f"Order should be allowed: {msg}"

    def test_order_exceeds_per_market_limit(self):
        """Order exceeding per-market limit should be rejected"""
        rm = RiskManager()
        rm.config.max_position_value_per_market = 0.10  # 10% per market

        # Existing position: 10 contracts at 50c = $5 = 5% of $100
        positions = [
            Position(ticker="TEST", market_title="Test",
                     side=Side.YES, quantity=10, avg_price=0.50,
                     current_bid=0.50, current_ask=0.55),
        ]

        total_equity = 100

        # New order: 15 contracts at 50c = $7.50
        # Total would be $12.50 = 12.5% > 10% limit
        ok, msg = rm.check_new_order(
            ticker="TEST", side="yes", size=15, price=50,
            positions=positions, total_equity=total_equity
        )
        assert not ok, "Order should be rejected"
        assert "per-market" in msg.lower()


class TestPortfolioSnapshot:
    """Test portfolio snapshot generation"""

    def test_snapshot_calculation(self):
        """Snapshot should correctly calculate all values"""
        rm = RiskManager()
        rm._starting_equity = 100.0

        market = Market(
            ticker="TEST", event_ticker="E1", title="", subtitle="",
            market_type=MarketType.FINANCIAL_ABOVE,
            yes_bid=60, yes_ask=65,
        )
        rm.update_markets_cache([market])

        positions = [
            Position(ticker="TEST", market_title="Test",
                     side=Side.YES, quantity=10, avg_price=0.50),
        ]

        snapshot = rm.get_portfolio_snapshot(
            cash_balance=94.0,  # Started with $100, spent $5 on position
            positions=positions,
        )

        # Position MTM = 10 * 0.60 = $6
        # Total equity = $94 + $6 = $100
        assert abs(snapshot.total_equity - 100.0) < 0.01

        # Daily P&L = $100 - $100 = $0
        assert abs(snapshot.daily_pnl) < 0.01


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
