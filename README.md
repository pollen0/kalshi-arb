# Kalshi Arbitrage

Arbitrage trading system for Kalshi prediction markets. Compares Kalshi range markets against futures pricing (NQ, ES) to identify mispricings.

## Features

- **Real-time futures data** via Yahoo Finance (free)
- **Fair value calculation** using probability models
- **Position tracking** with P&L vs market vs model pricing
- **Clean dark UI** dashboard

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up credentials
cp credentials.template.py credentials.py
# Edit credentials.py with your Kalshi API key and private key

# Run dashboard
python app.py

# Open http://localhost:5050
```

## Project Structure

```
├── app.py                    # Main entry point
├── credentials.py            # API keys (gitignored)
├── requirements.txt
│
└── src/
    ├── core/
    │   ├── client.py         # Kalshi API client (RSA-PSS auth)
    │   └── models.py         # Data models (Position, Market, Orderbook)
    │
    ├── financial/
    │   ├── futures.py        # Yahoo Finance client (NQ, ES)
    │   └── fair_value.py     # Range probability calculations
    │
    └── web/
        └── app.py            # Flask dashboard
```

## Dashboard

The unified dashboard displays:

| Tab | Content |
|-----|---------|
| **Positions** | Your open positions with entry price, current market, model fair value, and P&L |
| **NASDAQ-100** | All KXNASDAQ100 range markets with edge vs NQ futures |
| **S&P 500** | All KXINX range markets with edge vs ES futures |

Stats bar shows:
- Account balance
- Position count
- Total unrealized P&L
- Best edge opportunity

## Credential Setup

### Kalshi API

1. Log in to [Kalshi](https://kalshi.com)
2. Go to **Settings** > **API**
3. Click **Create API Key** with Trading permission
4. Download your private key file
5. Add to `credentials.py`:

```python
KALSHI_API_KEY = "your-api-key"

KALSHI_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
...your private key content...
-----END RSA PRIVATE KEY-----"""
```

## Strategy

The overnight arbitrage strategy:

1. **Futures trade nearly 24/7** (NQ, ES)
2. **Kalshi has low overnight volume** - prices become stale
3. **When futures move overnight**, Kalshi misprices range markets
4. **Place limit orders at fair value** to capture edge when markets reprice

## API Rate Limits

| Tier | Reads/sec | Writes/sec |
|------|-----------|------------|
| Basic | 20 | 10 |
| Advanced | 30 | 30 |
| Premier | 100 | 100 |

This system uses ~10 reads/sec, well within Basic tier limits.

## Risk Warning

**This system involves real money trading.**

- Prediction markets involve significant risk
- Only trade with money you can afford to lose
- Past performance does not guarantee future results

## License

MIT License - Use at your own risk.
