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

1. **Daily loss limit**: `current_nav < initial_nav * (1 - 0.03)`
   - `current_nav` = `bookkeeper.available_cash() + shares_held * last_close`
2. **Post-hours idle**: `status.position == "IDLE"` AND `current_time_EST > STOP_TRADING_TIME` (default 15:30 EST)
3. *(Further stop conditions to be added here as they are defined)*

> **Implementation note**: stop conditions must be evaluated before any branch logic below. If a stop condition is met, any pending orders are left as-is (do not cancel — cancelation is a trader action that requires a separate order).

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
> **Edge case**: if `i == 0` (no pivot below), do not issue Order B. Log a warning. Do not issue Order A either — entering without a stop-loss is not permitted.

**State updates** (via returned `AnalystOrder` — trader applies them):
- `status.watermark_level = i`
- `status.position = "LONG"` (set only after Order A confirmed — handled by bookkeeper/trader, not analyst)

---

## When Machine is LONG

### Step 1 — Check for watermark advance
- If `bar.close > pivots[watermark_level + 1]`:
  - Update: `watermark_level += 1`
  - Emit: `AnalystOrder(type="UPDATE_STOPLOSS", new_price=(pivots[watermark_level] + pivots[watermark_level - 1]) / 2)`
  - Rationale: trail the stop-loss upward as price rises through pivots

> **Design note / open question**: an alternative entry strategy considered but deferred is setting the stop-loss at `(pivot[watermark] + pivot[watermark+1]) / 2` (midpoint above instead of below). This would capture a small gain on exit but risks early exit during a bull run. Current choice: use the pivot *below*. Revisit after initial backtesting.

> **Edge case**: if `watermark_level + 1 >= len(pivots)` (at the top of the pivot list), do not advance. Log a warning. Consider this a signal to review pivot recalculation.

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

## Unresolved / To Be Defined

- [ ] Additional stop conditions (beyond daily loss and post-hours idle)
- [ ] Behaviour if a SELL_LIMIT stop-loss is cancelled externally (e.g. broker disconnect)
- [ ] Re-entry logic after a stop-loss is hit within the same day
- [ ] Handling of partial fills from the broker
- [ ] What happens if pivot list is empty (no pivots within ±12% range)
