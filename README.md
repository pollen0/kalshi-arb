# Kalshi Arbitrage Trading System

An automated trading system for finding and exploiting arbitrage opportunities on [Kalshi](https://kalshi.com) prediction markets.

## Features

### Sports Betting Arbitrage (`auto_trader.py`)
- **Multi-source probability consensus** - Combines odds from ESPN FPI, DraftKings, FanDuel, Pinnacle, and BetMGM
- **Automatic entry/exit** - Enters positions when edge exceeds threshold, exits on stop-loss or when edge disappears
- **Kelly criterion position sizing** - Optimal bet sizing based on edge and bankroll
- **Live game support** - Adjusts parameters for in-play betting
- **Web dashboard** - Real-time monitoring at `http://127.0.0.1:5050`
- **Supports NFL, NBA, NCAAF, and NCAAB** markets

### SPX Options Arbitrage (`spx_arb.py`)
- Compares Kalshi SPX daily range markets against SPY 0DTE options
- Uses Interactive Brokers for real-time options data
- Calculates implied probabilities from option spreads

## Requirements

- Python 3.8+
- Kalshi account with API access
- The Odds API key (for sports betting)
- Interactive Brokers TWS/Gateway (for SPX arbitrage only)

## Installation

### 1. Clone the repository

```bash
git clone git@github.com:pollen0/kalshi-arb.git
cd kalshi-arb
```

### 2. Install dependencies

```bash
pip3 install flask requests cryptography
```

For SPX arbitrage (optional):
```bash
pip3 install ib_insync
```

### 3. Set up credentials

Copy the template and add your API keys:

```bash
cp credentials.template.py credentials.py
```

Edit `credentials.py` with your Kalshi API credentials:

```python
KALSHI_API_KEY = "your-api-key-here"

KALSHI_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
YOUR_PRIVATE_KEY_HERE
-----END RSA PRIVATE KEY-----"""
```

#### Getting Kalshi API Credentials

1. Log in to [Kalshi](https://kalshi.com)
2. Go to **Settings** > **API**
3. Click **Create API Key**
4. Select permissions (enable trading for live trading)
5. Download your private key file
6. Copy the API key and private key contents to `credentials.py`

### 4. Get The Odds API Key

1. Sign up at [The Odds API](https://the-odds-api.com/)
2. Get your free API key (500 requests/month on free tier)
3. Set the environment variable or update `start.sh`:

```bash
export ODDS_API_KEY="your-odds-api-key"
```

## Usage

### Sports Betting Auto-Trader

**Start the system:**
```bash
./start.sh
```

Or manually:
```bash
export ODDS_API_KEY="your-key"
python3 auto_trader.py
```

**Access the dashboard:**
Open `http://127.0.0.1:5050` in your browser

**Stop the system:**
```bash
./stop.sh
```

**View logs:**
```bash
tail -f trader.log
```

### SPX Options Arbitrage

Requires Interactive Brokers TWS or Gateway running with API enabled.

```bash
python3 spx_arb.py
```

**TWS/Gateway Setup:**
1. Enable API in TWS: Configure > API > Settings
2. Check "Enable ActiveX and Socket Clients"
3. Set Socket Port (default: 7497 for paper, 7496 for live)
4. Uncheck "Read-Only API"

## Configuration

Trading parameters can be adjusted in `auto_trader.py`:

```python
CONFIG = {
    # Entry criteria
    "min_edge_entry": 0.05,       # 5% minimum edge to enter
    "min_sources": 2,             # At least 2 sources must agree
    "max_source_spread": 0.08,    # Max 8% disagreement between sources
    "min_volume": 500,            # Minimum $500 market volume

    # Position sizing
    "max_position_pct": 0.15,     # Max 15% of bankroll per position
    "kelly_fraction": 0.25,       # Use 1/4 Kelly

    # Exit criteria
    "take_profit_edge": 0.01,     # Exit if edge drops below 1%
    "stop_loss_pct": 0.40,        # Exit if down 40% on position

    # Risk limits
    "max_positions": 8,           # Max concurrent positions
    "max_daily_loss": 0.20,       # Stop if down 20% of starting balance
    "scan_interval": 30,          # Seconds between scans
}
```

## How It Works

### Probability Consensus

The system gathers win probabilities from multiple sources:

| Source | Weight | Description |
|--------|--------|-------------|
| Pinnacle | 2.5x | Sharp bookmaker, most accurate |
| DraftKings | 1.5x | Major US sportsbook |
| FanDuel | 1.5x | Major US sportsbook |
| BetMGM | 1.2x | Major US sportsbook |
| ESPN FPI | 1.0x | Football Power Index model |

A weighted consensus probability is calculated. If this consensus exceeds the Kalshi ask price by the minimum edge threshold, a buy signal is generated.

### Entry Logic

A trade is entered when ALL conditions are met:
1. Edge (consensus - Kalshi ask) >= 5% (7% for live games)
2. At least 2 sources provide data
3. Sources agree within 8%
4. Market has $500+ volume

### Exit Logic

A position is exited when ANY condition is met:
1. **Take profit**: Edge drops below 1%
2. **Stop loss**: Position is down 40%
3. **Cut loss**: Consensus flips against position by 3%+

### Position Sizing

Uses fractional Kelly criterion:
```
Position % = (Edge / (1 - Consensus)) × 0.25
```

Capped at 15% of bankroll (10% for live games).

## Project Structure

```
kalshi-arb/
├── auto_trader.py          # Main sports betting system
├── spx_arb.py             # SPX options arbitrage scanner
├── kalshi_client.py       # Kalshi API client
├── credentials.py         # Your API credentials (not in git)
├── credentials.template.py # Template for credentials
├── start.sh               # Start script
├── stop.sh                # Stop script
├── trader.log             # Runtime logs (not in git)
└── README.md              # This file
```

## Risk Warning

**This system trades with real money.**

- Past performance does not guarantee future results
- Prediction markets involve significant risk
- Only trade with money you can afford to lose
- The authors are not responsible for any financial losses

## API Limits

- **Kalshi API**: No hard rate limits, but be reasonable
- **The Odds API**: 500 requests/month on free tier (system caches for 5 minutes)
- **ESPN API**: No authentication required, public endpoints

## Troubleshooting

### "Kalshi not connected"
- Check your API credentials in `credentials.py`
- Ensure your API key has trading permissions enabled
- Verify your private key is correctly formatted

### "No opportunities found"
- Check if there are active sports games
- The Odds API may have hit its rate limit
- Try during NFL/NBA game days

### "Insufficient balance"
- Deposit funds to your Kalshi account
- The system requires a minimum balance to trade

### IBKR Connection Failed (SPX arb)
- Ensure TWS or IB Gateway is running
- Check API is enabled in TWS settings
- Verify the port number (7497 paper, 7496 live)

## License

MIT License - Use at your own risk.

## Contributing

Pull requests welcome. Please ensure you don't commit any API keys or credentials.
