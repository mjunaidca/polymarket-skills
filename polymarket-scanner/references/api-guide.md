# Polymarket API Guide

## Two APIs

Polymarket exposes two separate APIs for different purposes:

### 1. Gamma API (Market Metadata)

- **Base URL:** `https://gamma-api.polymarket.com`
- **Auth:** None required
- **Purpose:** Market discovery, metadata, search, and filtering

#### Key Endpoints

**GET /markets** — List and search markets

| Parameter | Type | Description |
|-----------|------|-------------|
| `limit` | int | Max results (up to 100) |
| `offset` | int | Pagination offset |
| `active` | bool | Filter active markets |
| `closed` | bool | Filter closed markets |
| `order` | string | Sort field: `volume24hr`, `liquidity`, `endDate`, `startDate`, `createdAt` |
| `ascending` | bool | Sort direction |
| `tag_slug` | string | Filter by category tag |
| `slug` | string | Exact match on market slug |
| `id` | string | Exact match on market ID |

Example request:
```
GET https://gamma-api.polymarket.com/markets?limit=5&active=true&closed=false&order=volume24hr&ascending=false
```

Example response (array of objects):
```json
[
  {
    "id": "572481",
    "question": "Will Trump nominate Scott Bessent as the next Fed chair?",
    "slug": "will-trump-nominate-scott-bessent-as-the-next-fed-chair",
    "outcomes": "[\"Yes\", \"No\"]",
    "outcomePrices": "[\"0.0015\", \"0.9985\"]",
    "clobTokenIds": "[\"10749...\", \"88386...\"]",
    "volume24hr": 11394218.4,
    "volumeNum": 35997330.32,
    "liquidityNum": 1270782.68,
    "endDate": "2026-12-31T00:00:00Z",
    "active": true,
    "closed": false,
    "acceptingOrders": true,
    "negRisk": true,
    "description": "...",
    "conditionId": "0x...",
    "orderPriceMinTickSize": 0.001,
    "orderMinSize": 5
  }
]
```

Note: `outcomes`, `outcomePrices`, and `clobTokenIds` are JSON-encoded strings that need to be parsed.

### 2. CLOB API (Prices and Order Books)

- **Base URL:** `https://clob.polymarket.com`
- **Auth:** None for read-only; L1/L2 wallet auth for trading
- **Purpose:** Real-time prices, order books, trade execution

#### Python Client

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

client = ClobClient("https://clob.polymarket.com")
```

#### Read-Only Methods (No Auth)

| Method | Arguments | Returns |
|--------|-----------|---------|
| `get_midpoint(token_id)` | str | `{"mid": "0.55"}` |
| `get_spread(token_id)` | str | `{"spread": "0.02"}` |
| `get_price(token_id, side)` | str, "BUY"/"SELL" | `{"price": "0.54"}` |
| `get_last_trade_price(token_id)` | str | `{"price": "0.55", "side": "BUY"}` |
| `get_order_book(token_id)` | str | OrderBookSummary object |

#### Batch Methods

```python
from py_clob_client.clob_types import BookParams

params = [BookParams(token_id=id1), BookParams(token_id=id2)]
client.get_midpoints(params)     # {id1: "0.55", id2: "0.45"}
client.get_spreads(params)       # {id1: "0.02", id2: "0.01"}
client.get_last_trades_prices(params)  # {id1: {"price": ..., "side": ...}, ...}
```

#### OrderBookSummary Object

```python
ob = client.get_order_book(token_id)
ob.market      # condition ID
ob.asset_id    # token ID
ob.bids        # list of OrderSummary(price="0.54", size="100")
ob.asks        # list of OrderSummary(price="0.56", size="200")
ob.timestamp   # server timestamp
```

## Rate Limits

- Gamma API: No published rate limit, but excessive requests may be throttled. Use reasonable limits (1-2 requests/second).
- CLOB API: Read-only endpoints are generous. Trading endpoints have stricter limits tied to API key tier.

## Error Handling

| HTTP Code | Meaning | Action |
|-----------|---------|--------|
| 200 | Success | Parse response |
| 400 | Bad request | Check parameters |
| 404 | Not found | Token ID or slug may be invalid |
| 429 | Rate limited | Back off and retry after delay |
| 500 | Server error | Retry with exponential backoff |

## Token IDs

Token IDs are long numeric strings (70+ digits) that uniquely identify each outcome in a market. A binary YES/NO market has 2 token IDs. Multi-outcome markets have one per outcome.

The token ID is the primary key for interacting with the CLOB API. Get them from the Gamma API's `clobTokenIds` field or the CLOB API's `get_markets()` method.

## Connecting the APIs

1. **Gamma API** for discovery: search/filter markets, get metadata and token IDs
2. **CLOB API** for pricing: use token IDs from Gamma to query live prices, order books, and spreads
