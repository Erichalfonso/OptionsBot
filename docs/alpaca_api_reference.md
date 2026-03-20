# Alpaca Trading API Reference — Options Trading

## Authentication
```
Headers:
  APCA-API-KEY-ID: YOUR_KEY
  APCA-API-SECRET-KEY: YOUR_SECRET
```
- Paper and live accounts have SEPARATE credentials — cannot mix them
- Paper base URL: `https://paper-api.alpaca.markets`

## Paper Trading
- Default balance: $100,000
- Uses real-time market data
- Does NOT simulate: slippage, dividends, borrow fees, price improvements
- Partial fills possible (~10% of time)

## OCC Symbol Format
```
{TICKER}{YYMMDD}{C/P}{STRIKE_PADDED_8_DIGITS}

Examples:
  SPY250320P00657000  = SPY, Mar 20 2025, Put, $657.00
  QQQ250319C00602000  = QQQ, Mar 19 2025, Call, $602.00

Strike padding: price * 1000, zero-padded to 8 digits
```

## Options Order — Key Parameters
```json
{
  "symbol": "SPY250320P00657000",
  "qty": 2,
  "side": "buy",
  "type": "market",
  "time_in_force": "day"
}
```

| Parameter | Required | Notes |
|-----------|----------|-------|
| symbol | Yes | OCC format |
| qty | Yes | Whole numbers only, no fractional |
| side | Yes | "buy" or "sell" |
| type | Yes | "market" or "limit" |
| limit_price | If limit | Required for limit orders |
| time_in_force | Yes | Only "day" or "gtc" (no ioc/opg) |

- Cannot use `notional` for options (use `qty` instead)
- No bracket/OTO orders for options

## Python SDK (alpaca-py)

### Setup
```python
from alpaca.trading.client import TradingClient

client = TradingClient(
    api_key='YOUR_KEY',
    secret_key='YOUR_SECRET',
    paper=True
)
```

### Market Order
```python
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

order = MarketOrderRequest(
    symbol="SPY250320P00657000",
    qty=2,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY
)
result = client.submit_order(order_data=order)
```

### Limit Order
```python
from alpaca.trading.requests import LimitOrderRequest

order = LimitOrderRequest(
    symbol="SPY250320P00657000",
    qty=2,
    side=OrderSide.BUY,
    limit_price=2.50,
    time_in_force=TimeInForce.DAY
)
result = client.submit_order(order_data=order)
```

### Account Info
```python
account = client.get_account()
account.buying_power
account.equity
account.cash
account.options_buying_power  # options-specific
account.pattern_day_trader    # PDT flag
```

### Positions
```python
# Get all open positions
positions = client.get_all_positions()

# Close single position
client.close_position(symbol="SPY250320P00657000")

# Close all positions
client.close_all_positions(cancel_orders=True)
```

### Orders
```python
from alpaca.trading.requests import GetOrdersRequest

# Get open orders
orders = client.get_orders(filter=GetOrdersRequest(status="open"))

# Cancel order
client.cancel_order_by_id(order_id="some-id")

# Cancel all
client.cancel_orders()
```

## Options Approval Levels
| Level | What You Can Do |
|-------|----------------|
| 0 | Disabled |
| 1 | Covered calls, cash-secured puts |
| 2 | Level 1 + buy calls, buy puts |
| 3 | Level 1-2 + spreads, iron condors |

We need Level 2 minimum (buying calls and puts).

## Gotchas
1. Paper keys and live keys are separate — "unauthorized" means wrong key set
2. Options qty must be whole numbers
3. Only "day" and "gtc" for time_in_force
4. Assignments don't trigger WebSocket — must poll REST
5. Auto-exercise: ITM contracts at expiration exercised if ITM by $0.01+
6. PDT: paper trading enforces $25K minimum rule
7. Rate limit: 1,000 API calls/min
8. Order timeout doesn't mean order failed — may have been submitted
9. Exercise requests between market close and midnight are rejected
