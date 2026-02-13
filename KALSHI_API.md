# Kalshi API Knowledge Base

This document contains learned knowledge about the Kalshi API, including undocumented behaviors and quirks discovered during development.

## API Base URL

```
https://api.elections.kalshi.com/trade-api/v2
```

## Authentication

- **Method**: RSA-PSS with SHA256
- **Headers required**:
  - `KALSHI-ACCESS-KEY`: Your API key
  - `KALSHI-ACCESS-SIGNATURE`: Base64-encoded signature
  - `KALSHI-ACCESS-TIMESTAMP`: Unix timestamp in milliseconds
- **Signature format**: `sign(timestamp + method + path)`

## Rate Limits (Basic Tier)

- **Reads**: 20 requests/second
- **Writes**: 10 requests/second
- Recommended: 50ms minimum between requests

## Series Tickers (Financial Markets)

### NASDAQ-100 Markets
| Series | Type | Description |
|--------|------|-------------|
| `KXNASDAQ100` | Range | "Between X and Y" markets |
| `KXNASDAQ100U` | Above/Below | "X or above" markets |
| `KXNASDAQ100W` | Weekly | Weekly range markets |
| `KXNASDAQ100M` | Monthly | Monthly range markets |

### S&P 500 Markets
| Series | Type | Description |
|--------|------|-------------|
| `KXINX` | Range | "Between X and Y" markets |
| `KXINXU` | Above/Below | "X or above" markets |
| `KXINXW` | Weekly | Weekly range markets |
| `KXINXM` | Monthly | Monthly range markets |

### Treasury 10-Year Yield Markets
| Series | Type | Description |
|--------|------|-------------|
| `KXTNOTED` | Daily | 10Y Treasury yield prediction markets |

**Note**: Treasury markets use yield values (e.g., 4.50%) instead of price levels.

### USD/JPY Markets (Future)
| Series | Type | Description |
|--------|------|-------------|
| `KXUSDJPY` | Daily | USD/JPY exchange rate markets |

### EUR/USD Markets (Future)
| Series | Type | Description |
|--------|------|-------------|
| `KXEURUSD` | Daily | EUR/USD exchange rate markets |

## Ticker Format

```
{SERIES}-{DATE}H{TIME}-{TYPE}{STRIKE}

Examples:
KXNASDAQ100-26FEB04H1600-B25450    # Range: between 25,400-25,500
KXNASDAQ100U-26FEB04H1600-T25400   # Above: 25,400 or above
```

- **Date**: `YYMMMDD` format (e.g., `26FEB04` = Feb 4, 2026)
- **Time**: `HHMM` in EST 24-hour format (e.g., `H1600` = 4:00 PM EST)
- **Type prefix**:
  - `B` = Between/Range market
  - `T` = Threshold/Above market

## Market Status Values

| Status | Meaning | Tradable? |
|--------|---------|-----------|
| `initialized` | Created but not yet open | No |
| `active` | Open for trading | Yes |
| `closed` | Trading ended, awaiting settlement | No |
| `finalized` | Settled and paid out | No |

**Important Discovery**: Markets may show as `initialized` in the API even when tradable on the Kalshi website. This typically happens with "above/below" markets (`KXNASDAQ100U`, `KXINXU`) which may be in a different series than expected.

**API Status Filter Quirk (Feb 2026)**: The `/markets` endpoint rejects `status=active` with "invalid status filter". The value `open` is accepted as a query param, but the response body still uses `active` as the status label. To avoid compatibility issues, we omit the `status` query parameter entirely and filter client-side by `close_time`.

## Settlement Times

### Equity Index Markets (NASDAQ-100, S&P 500)
**Daily markets settle at 4:00 PM EST (market close)**

| Timezone | Settlement Time |
|----------|-----------------|
| EST (New York) | 4:00 PM |
| PST (Los Angeles) | 1:00 PM |
| UTC | 9:00 PM (21:00) |

### Treasury, Forex Markets
**Daily markets typically settle at 10:00 AM EST**

| Timezone | Settlement Time |
|----------|-----------------|
| EST (New York) | 10:00 AM |
| PST (Los Angeles) | 7:00 AM |
| UTC | 3:00 PM (15:00) |

## API Endpoints

### Get Markets
```
GET /markets?series_ticker={SERIES}&status={STATUS}&limit={LIMIT}
```

**Parameters**:
- `series_ticker`: Filter by series (e.g., `KXNASDAQ100`)
- `status`: Filter by status (`active`, `open`, `closed`, etc.)
- `event_ticker`: Filter by specific event
- `limit`: Max results (default varies)

**Response fields**:
```json
{
  "markets": [
    {
      "ticker": "KXNASDAQ100-26FEB04H1600-B25450",
      "status": "active",
      "yes_bid": 45,
      "yes_ask": 47,
      "no_bid": 53,
      "no_ask": 55,
      "floor_strike": 25400,
      "cap_strike": 25499.99,
      "close_time": "2026-02-04T21:00:00Z",
      "volume": 1234
    }
  ]
}
```

### Get Positions
```
GET /portfolio/positions
```

**Response fields**:
```json
{
  "market_positions": [
    {
      "ticker": "KXNASDAQ100-26FEB03H1600-B25250",
      "position": 5,           // Positive = YES, Negative = NO
      "average_price": 45,     // In cents
      "realized_pnl": 0,
      "market_title": "..."
    }
  ]
}
```

**Note**: `average_price` may be 0 for settled/settling markets.

### Place Order
```
POST /portfolio/orders
```

**Request body**:
```json
{
  "ticker": "KXNASDAQ100-26FEB04H1600-B25450",
  "action": "buy",
  "side": "yes",
  "count": 10,
  "type": "limit",
  "yes_price": 45,
  "client_order_id": "unique_id_123"
}
```

### Get Orderbook
```
GET /markets/{ticker}/orderbook
```

**Response**:
```json
{
  "orderbook": {
    "yes": [[45, 100], [44, 50]],  // [price, quantity]
    "no": [[55, 100], [56, 50]]
  }
}
```

## Known Issues & Quirks

### 1. Feb 4 vs Feb 6 Market Availability
- Range markets (`KXNASDAQ100`) may not be active for the next trading day
- Above/below markets (`KXNASDAQ100U`) often activate earlier
- Always check both series when looking for tomorrow's markets

### 2. Position `average_price` = 0
When a contract is settling or settled, the `average_price` field may return 0. This is normal behavior during settlement.

### 3. Market Price Display
- Kalshi website shows `yes_ask` as the "market price" for YES
- For accurate P&L, use bid for selling, ask for buying

### 4. Missing Markets
If expected markets don't appear with `status=active`:
1. Check `status=open` or no status filter
2. Try the alternative series (e.g., `KXNASDAQ100U` instead of `KXNASDAQ100`)
3. Markets may still be in `initialized` state

### 5. Date Gaps
Some dates may not have markets (e.g., weekends, holidays). The API doesn't indicate why a date is missing.

## Useful Queries

### Get all active markets for tomorrow
```python
# Check both range and above/below series
range_markets = client.get_markets(series_ticker="KXNASDAQ100", status="active")
above_markets = client.get_markets(series_ticker="KXNASDAQ100U", status="active")
```

### Check market liquidity
```python
# Low liquidity indicator: yes_bid == 0 or spread > 10c
has_liquidity = market.yes_bid > 0 and (market.yes_ask - market.yes_bid) < 10
```

### Find market-making opportunities
```python
# Markets with no bids are opportunities to set prices
for m in markets:
    if m.yes_bid == 0:
        print(f"No YES bids on {m.ticker} - you can set the price!")
```

## Market Making Strategy

When markets have thin liquidity (wide spreads like 1c/100c), you can act as a market maker:

### Identifying Opportunities
```python
# Wide spread + high fair value = opportunity
spread = yes_ask - yes_bid
if spread > 30 and fair_value > 0.70:
    # Place bid below fair value but above current bid
    suggested_bid = int(fair_value * 100 - 5)  # 5% safety margin
```

### Example (Feb 4, 2026 NASDAQ Above Markets)
```
Strike    | Fair Value | Bid/Ask  | Suggested Bid | Edge
23,600+   |    99.0%   |  1/100c  |     94c       |  5.0%
24,800+   |    85.0%   |  7/100c  |     79c       |  6.0%
```

### Key Insight
Above/below markets (KXNASDAQ100U, KXINXU) often have thinner liquidity than range markets, making them better for market making.

---
*Last updated: 2026-02-03*
*Added: Treasury 10Y markets (KXTNOTED), USD/JPY and EUR/USD series references*
*Add new discoveries to this file as they're found.*
