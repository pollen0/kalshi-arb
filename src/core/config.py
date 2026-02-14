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
class AssetClassConfig:
    """Asset class definitions and characteristics"""

    # Map series tickers to asset classes for correlated exposure
    series_to_asset_class: dict = field(default_factory=lambda: {
        "KXNASDAQ100": "nasdaq",
        "KXNASDAQ100U": "nasdaq",
        "KXINX": "spx",
        "KXINXU": "spx",
        "KXTNOTED": "treasury",
        "KXWTI": "wti",
        "KXWTIW": "wti",
        "KXBTC": "bitcoin",
        "KXBTC15M": "bitcoin",
        "KXBTCD": "bitcoin",
        "KXETH": "ethereum",
        "KXETH15M": "ethereum",
        "KXETHD": "ethereum",
        "KXSOL": "solana",
        "KXSOL15M": "solana",
        "KXSOLD": "solana",
        "KXDOGE": "dogecoin",
        "KXDOGE15M": "dogecoin",
        "KXDOGED": "dogecoin",
        "KXXRP": "xrp",
        "KXXRP15M": "xrp",
        "KXXRPD": "xrp",
    })

    # Trading hours per day by asset class (for time conversion)
    trading_hours_per_day: dict = field(default_factory=lambda: {
        "equity": 6.5,      # 9:30 AM - 4:00 PM ET
        "futures": 23.0,    # Nearly 24-hour trading
        "forex": 24.0,      # 24-hour market
        "treasury": 6.5,    # Similar to equity hours
        "commodity": 23.0,  # Oil trades ~23h/day
        "crypto": 24.0,     # 24/7 market
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
        "wti": "commodity",
        "bitcoin": "crypto",
        "ethereum": "crypto",
        "solana": "crypto",
        "dogecoin": "crypto",
        "xrp": "crypto",
    })

    # Typical volatility ratios between related assets
    volatility_ratios: dict = field(default_factory=lambda: {
        "nasdaq_to_spx": 1.25,  # NDX typically 25% more volatile than SPX
        "russell_to_spx": 1.35, # RUT typically 35% more volatile than SPX
    })


@dataclass
class RiskConfig:
    """Risk management configuration"""

    # === Shared risk ===
    max_daily_loss_pct: float = 0.10            # Stop if down 10% (relaxed for small accounts)
    max_drawdown_pct: float = 0.15              # Max 15% drawdown from peak equity
    max_total_exposure: float = 0.80            # Max 80% total exposure

    # === Strategy-specific defaults ===
    financial_min_edge: float = 0.02            # Financial: 2% min edge
    financial_position_size: int = 10           # Financial: 10 contracts

    # === Non-UI settings (kept as hardcoded defaults) ===
    warning_daily_loss_pct: float = 0.03        # Warning at 3%
    max_exposure_per_asset_class: float = 0.30  # Max 30% in any one asset class
    max_position_per_market: int = 100          # Max contracts per market
    max_position_value_per_market: float = 0.10 # Max 10% of equity per market
    max_order_age_seconds: int = 300            # Cancel orders older than 5 minutes
    data_delay_buffer: float = 0.005            # Extra 0.5% edge for data delay
    max_price_staleness_seconds: float = 5.0    # Reject prices older than 5 seconds


# Global configuration instances
_asset_class_config: Optional[AssetClassConfig] = None
_risk_config: Optional[RiskConfig] = None
_config_lock = threading.Lock()


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
        ):
            if key in data:
                setattr(new_config, key, type(getattr(new_config, key))(data[key]))

        # Atomic swap
        _risk_config = new_config

        print(f"[CONFIG] Loaded trading config from {config_path}")
    except Exception as e:
        print(f"[CONFIG] Failed to load config: {e}")


