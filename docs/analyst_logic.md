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
- `bookkeeper: Bookkeeper` — current bookkeeper state
- `recalc_hook: Callable[[], List[float]] | None = None` — optional callback supplied by the entry point; called when `bar.close` moves out of pivot scope to attempt an immediate pivot rebuild before falling back to virtual grid extension. `None` in contexts where on-demand rebuild is not supported (e.g., batch mode without a live DB connection).

## Output

A list of `AnalystOrder` objects. May be empty. Processed in order by the trader.

---

## Out-of-Scope Pivot Handling

Executed on every bar, immediately after the empty-pivot guard and before any branch logic.

### Boundary Definitions

A price is **within scope** if:

```
lower_boundary = min_pivot × (1 − 0.0075)   # min_pivot × (1 − 0.015 / 2)
upper_boundary = max_pivot × (1 + 0.0075)   # max_pivot × (1 + 0.015 / 2)
```

If `bar.close` falls outside these boundaries the price is **out of scope (OOS)**.

### Step 1 — Recalculation Trigger

When OOS is first detected:
- If `recalc_hook` is provided, call it immediately to obtain fresh pivots.
- If the fresh pivots cover `bar.close` (i.e. close is now in scope), use them — no virtual extension needed.
- If the fresh pivots still do not cover `bar.close`, or no hook was provided, proceed to Step 2.

> `recalc_hook` must invoke the pivot builder with the same 3-month lookback used at scheduled 30-bar checkpoints (see `stream_entry.on_bar()`).

### Step 2 — Virtual Pivot Grid Extension

Extend the pivot list geometrically at **1.5% spacing** until the closest virtual pivot covers `bar.close`.

**Price above scope** — append upward:
```
pivot[len + j − 1] = max_pivot × (1 + 0.015)^j
j = max(1, round(log(bar.close / max_pivot) / log(1.015)))
Append pivots for j = 1 … j_target (list stays sorted ascending).
```

**Price below scope** — prepend downward:
```
pivot[−j] = min_pivot × (1 − 0.015)^j
j = max(1, round(log(bar.close / min_pivot) / log(0.985)))
Prepend pivots for j = j_target … 1 (list stays sorted ascending).
```

> The extended list is **ephemeral** — local to the current bar's analysis call. It is not written back to the session pivot cache or stored in `MachineStatus`.

### Effect on Downstream Logic

After this step the working pivot list always contains `bar.close` within its boundaries. The IDLE and LONG branches below operate on the resolved list without any special-case guarding for price extremes.

---

## Stop Conditions (checked first, every bar)

If **any** of the following are true, the analyst emits **no orders** and returns immediately:

1. **Daily loss limit**: `status.position == "IDLE"` AND `available_cash < initial_cash * (1 - 0.03)` #at opening available_cash = initial_nav
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
> **Edge case**: if `i == 0` (no pivot below), prepend one virtual pivot at `pivots[0] × (1 − 0.015)` to the working pivot list and set `i = 1`. The original `pivots[0]` becomes `pivots[1]` in the extended list. Order B price = `(pivots[1] + pivots[0]) / 2` in the extended list (midpoint between the original lowest pivot and the new virtual pivot). No special-casing in order construction is needed.

**State updates** (via returned `AnalystOrder` — trader applies them):
- `status.watermark_level = i`
- `status.position = "LONG"` (set only after Order A confirmed — handled by bookkeeper/trader, not analyst)

---

## When Machine is LONG

Before the watermark advance check, verify that a pending stop-loss order is still active (`len(status.pending_order_ids) > 0`). Under the single-position constraint every pending order while LONG is a SELL; an empty list means the stop-loss was unexpectedly removed (e.g. broker-side cancellation).

**If stop-loss is missing** (`status.pending_order_ids` is empty):
- Re-emit a `SELL_LIMIT` stop-loss anchored to the **closest pivot to `bar.close`**, not the original watermark. This ensures the protection level reflects current price rather than the stale entry level.
  - `price = (pivots[i] + pivots[i−1]) / 2`, where `i` = index of pivot closest to `bar.close`
- **Exception**: if the closest pivot is at index 0 (no pivot below), re-emission is skipped and a warning is logged.
- Continue to Step 1 below. A re-emitted `SELL_LIMIT` and an `UPDATE_STOPLOSS` may appear together in the same bar's output.

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

## Order Attribution (UUID Convention)

Every `BUY_LIMIT` order carries a UUID generated by the analyst in its `condition` field. The paired `SELL_LIMIT` stop-loss references that UUID in its `condition` field as `on_fill:<uuid>`.

**The Trader must use the value in `BUY_LIMIT.condition` as the `BrokerOrder.order_id`** for that buy. This guarantees that the stop-loss `condition` string matches the live broker order ID at execution time.

```
Analyst emits:
  BUY_LIMIT  → condition = "<uuid>"          # Trader uses this as BrokerOrder.order_id
  SELL_LIMIT → condition = "on_fill:<uuid>"  # references the BUY order by the same ID
```

This convention is the only mechanism linking a stop-loss to its paired buy order. No other order type carries a UUID in `condition`.

---

## Other Cases / Unexpected behaviour
###


## Unresolved / To Be Defined

- [ ] Re-entry logic after a stop-loss is hit within the same day: when the stop-loss confirmation is received, the bookkeeper should reconfirm the cash available with the broker, update the machine status to idle and then proceed with the normal entry logic 
- [ ] Handling of partial fills of BUY_LIMIT orders from the broker: cancel the unfilled part of the order and adjust the position size of the stop loss accordingly 
- [ ] Handling of partial fills of SELL_LIMIT orders from the broker: keep the unfilled part as a SELL order and keep the machine status LONG untill the entire position has been sold - in the meantime the same LONG logic is applied  
- [ ] What happens if pivot list is empty (no pivots within ±12% range)- issue a warning
