# Analyst Logic — Source of Truth

This document is the authoritative specification for the analyst's behaviour.
The implementation in `src/analyst/` must match this document exactly.
Any deviation is a bug. Any desired change must be made here first.

---

## Inputs

The analyst receives on every bar:
- `bar: OHLCVBar` — the latest closed bar
- `status: MachineStatus` — current machine state
- `pivots: List[float]` — current pivot list, sorted ascending (`pivots[j] < pivots[j+1]`)
- `recent_bars: List[OHLCVBar]` — last 5 bars (for VWAP calculation)

## Output

A list of `AnalystOrder` objects. May be empty. Processed in order by the trader.

---

## Stop Conditions (checked first, every bar)

If **any** of the following are true, the analyst emits **no orders** and returns immediately:

1. **Daily loss limit**: `status.position == "IDLE"` AND `available_cash < initial_cash * (1 - 0.03)`
   - `available_cash` = `bookkeeper.available_cash()`
2. **Post-hours idle**: `status.position == "IDLE"` AND `current_time_EST > STOP_TRADING_TIME` (default 15:30 EST)
3. *(Further stop conditions to be added here as they are defined)*

> **Implementation note**: stop conditions must be evaluated before any branch logic below. If a stop condition is met, cancel any pending orders and return immediately.

---

## When Machine is IDLE

### Step 1 — Cancel stale buy orders
- Emit: `AnalystOrder(type="CANCEL_ALL_BUYS")`
- Rationale: clears any unexecuted buy orders from prior bars before reassessing

### Step 2 — Identify candidate pivot
- Compute **VWAP of last 5 bars**: `sum(typical_price_i * volume_i) / sum(volume_i)` where `typical_price = (high + low + close) / 3`
- Find `pivot_vwap` = pivot in `pivots` closest to that VWAP
- Find `pivot_close` = pivot in `pivots` closest to `bar.close`
- If `pivot_vwap != pivot_close`: emit nothing further, return. Wait for next bar.
- If `pivot_vwap == pivot_close` (same pivot, call it `pivots[i]`): proceed to Step 3

### Step 3 — Issue entry orders
Let `i` = index of the identified pivot in `pivots`.

**Order A — Buy limit**:
```
type:        BUY_LIMIT
max_price:   pivots[i] * (1 - 0.001)    # 0.1% below the pivot
size:        FULL_AVAILABLE_CASH
```

**Order B — Conditional stop-loss** (conditioned on Order A executing):
```
type:        SELL_LIMIT
condition:   on_fill(order_A)
price:       (pivots[i] + pivots[i-1]) / 2    # midpoint between pivot[i] and pivot below
size:        ALL_SHARES
```
> **Edge case**: if `i == 0` (no pivot below),  issue Order B with price = `pivots[0] * (1 - 0.015)` (1.5% below the pivot).

**State updates** (via returned `AnalystOrder` — trader applies them):
- `status.watermark_level = i`
- `status.position = "LONG"` (set only after Order A confirmed — handled by bookkeeper/trader, not analyst)

---

## When Machine is LONG

First check if a pending stop-loss order is still active (it always should, but this is to handle any unexèpected behaviour like cancellation from the broker side). If not, emit a new stop-loss order at the current watermark level and continue with the flow below, otherwise just continue with the flow below. 

### Step 1 — Check for watermark advance
- If `bar.close > pivots[watermark_level + 1]`:
  - Update: `watermark_level += 1`
  - Emit: `AnalystOrder(type="UPDATE_STOPLOSS", price=(pivots[watermark_level] + pivots[watermark_level - 1]) / 2)`
  - Rationale: trail the stop-loss upward as price rises through pivots

> **Edge case**: if `watermark_level + 1 >= len(pivots)` (at the top of the pivot list), update stoploss with `price=bar.close * (1 + 0.015)` — `bar.close` is the last known price (set new watermark every 1.5% gain).

### Step 2 — Check for end-of-day exit
- If `current_time_EST > STOP_TRADING_TIME`:
  - Emit: `AnalystOrder(type="SELL_MARKET", size="ALL_SHARES")`
  - Rationale: do not hold positions overnight

### Step 3 — Otherwise
- Emit nothing. The existing stop-loss order (placed when entering LONG) remains active with the broker.

---

## Constants (to be defined in `src/config.py`)

| Name | Default | Description |
|---|---|---|
| `STOP_TRADING_TIME` | 15:30 EST | No new orders after this time |
| `DAILY_LOSS_LIMIT` | 0.03 | 3% of initial NAV |
| `MIN_CASH_RESERVE` | 100.0 | USD, never deployed |
| `BUY_LIMIT_OFFSET` | 0.001 | 0.1% below pivot |
| `PIVOT_RANGE` | 0.12 | ±12% of last close |
| `PIVOT_INTERVAL_BARS` | 30 | Bars between pivot recalculations |
| `PIVOT_LOOKBACK_DAYS` | 90 | ~3 months of history for pivot builder |
| `VWAP_WINDOW_BARS` | 5 | Bars used for VWAP in idle check |

---

## Other Cases / Unexpected behaviour
###


## Unresolved / To Be Defined

- [ ] Re-entry logic after a stop-loss is hit within the same day: when the stop-loss confirmation is received, the bookkeeper should reconfirm the cash available with the broker, update the machine status to idle and then proceed with the normal entry logic 
- [ ] Handling of partial fills of BUY_LIMIT orders from the broker: cancel the unfilled part of the order and adjust the position size of the stop loss accordingly 
- [ ] Handling of partial fills of SELL_LIMIT orders from the broker: keep the unfilled part as a SELL order and keep the machine status LONG untill the entire position has been sold - in the meantime the same LONG logic is applied  
- [ ] What happens if pivot list is empty (no pivots within ±12% range)- issue a warning
