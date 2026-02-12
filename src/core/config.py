"""
Centralized Configuration for Kalshi Arbitrage System

Contains settlement instruments, asset class mappings, and system-wide settings.
"""

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trading_config.json")


@dataclass
class SettlementConfig:
    """Configuration for settlement instruments and pricing sources"""

    # Use cash index (not futures) for settlement reference
    # Kalshi settles to the cash index, not futures
    use_index_for_settlement: bool = True

    # Settlement instruments by Kalshi series
    # These are the actual instruments Kalshi settles to
    settlement_instruments: dict = field(default_factory=lambda: {
        # NASDAQ-100 markets settle to ^NDX (cash index)
        "KXNASDAQ100": "^NDX",
        "KXNASDAQ100U": "^NDX",
        # S&P 500 markets settle to ^GSPC (cash index)
        "KXINX": "^GSPC",
        "KXINXU": "^GSPC",
        # Treasury settles to actual yield
        "KXTNOTED": "^TNX",
    })

    # Futures symbols for real-time price tracking
    # These trade when markets are closed and provide price discovery
    futures_symbols: dict = field(default_factory=lambda: {
        "nasdaq": "NQ=F",    # E-mini NASDAQ-100 futures
        "spx": "ES=F",       # E-mini S&P 500 futures
        "dow": "YM=F",       # E-mini Dow futures
        "russell": "RTY=F",  # E-mini Russell 2000 futures
    })

    # Cash index symbols (what Kalshi actually settles to)
    index_symbols: dict = field(default_factory=lambda: {
        "nasdaq": "^NDX",    # NASDAQ-100 Index
        "spx": "^GSPC",      # S&P 500 Index
        "dow": "^DJI",       # Dow Jones Industrial Average
        "russell": "^RUT",   # Russell 2000 Index
    })

    # Options symbols for implied volatility
    options_symbols: dict = field(default_factory=lambda: {
        "nasdaq": "QQQ",     # NASDAQ-100 ETF options (most liquid)
        "spx": "SPY",        # S&P 500 ETF options (most liquid)
        # Can also use SPX/NDX index options but less liquid for 0DTE
    })

    # VIX and volatility indices
    volatility_indices: dict = field(default_factory=lambda: {
        "vix": "^VIX",       # S&P 500 implied volatility
        "vxn": "^VXN",       # NASDAQ-100 implied volatility
    })


@dataclass
class AssetClassConfig:
    """Asset class definitions and characteristics"""

    # Map series tickers to asset classes for correlated exposure
    series_to_asset_class: dict = field(default_factory=lambda: {
        "KXNASDAQ100": "nasdaq",
        "KXNASDAQ100U": "nasdaq",
        "KXINX": "spx",
        "KXINXU": "spx",
        "KXTNOTED": "treasury",
    })

    # Trading hours per day by asset class (for time conversion)
    trading_hours_per_day: dict = field(default_factory=lambda: {
        "equity": 6.5,      # 9:30 AM - 4:00 PM ET
        "futures": 23.0,    # Nearly 24-hour trading
        "forex": 24.0,      # 24-hour market
        "treasury": 6.5,    # Similar to equity hours
    })

    # Asset class types for volatility modeling
    asset_class_types: dict = field(default_factory=lambda: {
        "nasdaq": "equity",
        "spx": "equity",
        "dow": "equity",
        "russell": "equity",
        "treasury10y": "yield",
        "usdjpy": "forex",
        "eurusd": "forex",
    })

    # Typical volatility ratios between related assets
    volatility_ratios: dict = field(default_factory=lambda: {
        "nasdaq_to_spx": 1.25,  # NDX typically 25% more volatile than SPX
        "russell_to_spx": 1.35, # RUT typically 35% more volatile than SPX
    })


@dataclass
class RiskConfig:
    """Risk management configuration — shared across financial and sports systems"""

    # === Shared risk (applies to both financial + sports) ===
    max_daily_loss_pct: float = 0.05            # Stop if down 5%
    max_drawdown_pct: float = 0.15              # Max 15% drawdown from peak equity
    max_total_exposure: float = 0.80            # Max 80% total exposure

    # === Strategy-specific defaults ===
    financial_min_edge: float = 0.02            # Financial: 2% min edge
    financial_position_size: int = 10           # Financial: 10 contracts
    sports_min_edge: float = 0.05              # Sports: 5% min edge
    sports_position_size: int = 20             # Sports: 20 contracts (max bet size)

    # === Non-UI settings (kept as hardcoded defaults) ===
    warning_daily_loss_pct: float = 0.03        # Warning at 3%
    max_exposure_per_asset_class: float = 0.30  # Max 30% in any one asset class
    max_position_per_market: int = 100          # Max contracts per market
    max_position_value_per_market: float = 0.10 # Max 10% of equity per market
    max_order_age_seconds: int = 300            # Cancel orders older than 5 minutes
    data_delay_buffer: float = 0.005            # Extra 0.5% edge for data delay
    max_price_staleness_seconds: float = 5.0    # Reject prices older than 5 seconds


@dataclass
class ModelConfig:
    """Fair value model configuration"""

    # Distribution settings
    use_fat_tails: bool = True                  # Use Student's t instead of normal
    tail_degrees_of_freedom: int = 5            # df for Student's t (lower = fatter tails)

    # Volatility sources (in order of preference)
    volatility_preference: list = field(default_factory=lambda: [
        "options_implied",      # 1st: Options-implied volatility
        "vix_scaled",           # 2nd: VIX-based estimate
        "historical",           # 3rd: Historical volatility
        "default",              # 4th: Default assumptions
    ])

    # Options-based model settings
    use_options_skew: bool = True               # Apply put-call skew adjustment
    use_options_implied_distribution: bool = True  # Use risk-neutral probabilities from options

    # Minimum data requirements
    min_options_volume: int = 100               # Min volume for option quotes to be valid
    min_options_open_interest: int = 500        # Min OI for option quotes

    # Sanity checks
    max_fair_value_deviation: float = 0.25      # Max 25% deviation from market mid


# Global configuration instances
_settlement_config: Optional[SettlementConfig] = None
_asset_class_config: Optional[AssetClassConfig] = None
_risk_config: Optional[RiskConfig] = None
_model_config: Optional[ModelConfig] = None
_config_lock = threading.Lock()


def get_settlement_config() -> SettlementConfig:
    """Get global settlement configuration"""
    global _settlement_config
    if _settlement_config is None:
        with _config_lock:
            if _settlement_config is None:
                _settlement_config = SettlementConfig()
    return _settlement_config


def get_asset_class_config() -> AssetClassConfig:
    """Get global asset class configuration"""
    global _asset_class_config
    if _asset_class_config is None:
        with _config_lock:
            if _asset_class_config is None:
                _asset_class_config = AssetClassConfig()
    return _asset_class_config


def get_risk_config() -> RiskConfig:
    """Get global risk configuration (loads persisted config on first call)"""
    global _risk_config
    if _risk_config is None:
        with _config_lock:
            if _risk_config is None:
                _risk_config = RiskConfig()
                load_config()  # Apply persisted overrides
    return _risk_config


def save_config():
    """Persist the UI-editable fields of RiskConfig to data/trading_config.json.

    Reads current values under the lock, then writes to disk outside the lock
    (disk I/O should not hold the lock). Uses atomic tmp+replace write.
    """
    with _config_lock:
        rc = get_risk_config()
        data = {
            "max_daily_loss_pct": rc.max_daily_loss_pct,
            "max_drawdown_pct": rc.max_drawdown_pct,
            "max_total_exposure": rc.max_total_exposure,
            "financial_min_edge": rc.financial_min_edge,
            "financial_position_size": rc.financial_position_size,
            "sports_min_edge": rc.sports_min_edge,
            "sports_position_size": rc.sports_position_size,
        }
    config_path = os.path.abspath(_CONFIG_FILE)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, config_path)
    print(f"[CONFIG] Saved trading config to {config_path}")


def load_config():
    """Load persisted config overrides from data/trading_config.json into the global RiskConfig.

    Builds a new RiskConfig with overrides applied, then atomically swaps the global reference.
    """
    global _risk_config
    if _risk_config is None:
        _risk_config = RiskConfig()

    config_path = os.path.abspath(_CONFIG_FILE)
    if not os.path.exists(config_path):
        return

    try:
        with open(config_path, "r") as f:
            data = json.load(f)

        # Build new config with overrides applied (swap-entire-object pattern)
        new_config = RiskConfig()
        # Copy current non-UI fields from existing config
        current = _risk_config
        for fld in current.__dataclass_fields__:
            setattr(new_config, fld, getattr(current, fld))

        # Apply persisted overrides
        for key in (
            "max_daily_loss_pct", "max_drawdown_pct", "max_total_exposure",
            "financial_min_edge", "financial_position_size",
            "sports_min_edge", "sports_position_size",
        ):
            if key in data:
                setattr(new_config, key, type(getattr(new_config, key))(data[key]))

        # Atomic swap
        _risk_config = new_config

        print(f"[CONFIG] Loaded trading config from {config_path}")
    except Exception as e:
        print(f"[CONFIG] Failed to load config: {e}")


def get_model_config() -> ModelConfig:
    """Get global model configuration"""
    global _model_config
    if _model_config is None:
        _model_config = ModelConfig()
    return _model_config
