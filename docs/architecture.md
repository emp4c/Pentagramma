# Architecture — Algo Trader

## Overview

An automated trading algorithm that operates in two modes:
- **Streaming mode** (production): receives one OHLCV bar per minute from a broker API
- **Batch mode** (development/testing): receives a date range, replays historical bars from the long-term DB, simulating "present" moving forward bar by bar

The architecture is a pipeline of stateless components communicating via explicit interfaces. No shared mutable state. The `TradingMachine` owns all runtime state and passes it explicitly to each component.

---

## Component Map

```
                        ┌──────────────────────────────────┐
                        │           Entry Interface         │
                        │  stream_entry  |  batch_runner    │
                        │  (src/entry)   |  (dev_tools/)    │
                        └──────────────┬───────────────────┘
                                       │ OHLCVBar (one at a time)
                                       ▼
                        ┌──────────────────────────────────┐
                        │         Pivot Builder             │
                        │  (src/pivot)                      │
                        │  runs every 30 bars               │
                        │  returns List[float] sorted asc   │
                        └──────────────┬───────────────────┘
                                       │ pivots: List[float]
                                       ▼
                        ┌──────────────────────────────────┐
                        │         Trading Machine           │
                        │  owns: MachineStatus              │
                        │                                   │
                        │  ┌─────────┐    ┌─────────────┐  │
                        │  │Analyst  │───▶│   Trader    │  │
                        │  └─────────┘    └──────┬──────┘  │
                        │                        │ Order   │
                        │  ┌─────────────────────┼──────┐  │
                        │  │    Bookkeeper        │      │  │
                        │  └─────────────────────┼──────┘  │
                        └────────────────────────┼─────────┘
                                                 │ Order
                                                 ▼
                        ┌──────────────────────────────────┐
                        │           Broker Bus              │
                        │  (src/bus)                        │
                        │  streaming: real broker API       │
                        │  batch:     test_api (dev_tools)  │
                        └──────────────┬───────────────────┘
                                       │ ExecutionConfirmation
                                       ▼
                        ┌──────────────────────────────────┐
                        │          Bookkeeper               │
                        │  (src/bookkeeper)                 │
                        │  ledger, cash, shares tracking    │
                        └──────────────────────────────────┘
```

---

## Components — Responsibilities & Interfaces

### Entry Interface (`src/entry/`)
- **stream_entry.py**: called by the live data feed; receives one `OHLCVBar`, triggers the pipeline
- Maintains a bar counter to know when to call the pivot builder (every 30 bars)

### Batch Runner (`dev_tools/batch_runner/`)
- **Not production code**
- Accepts `(ticker, start_date, end_date)` — slices the long-term OHLCV DB internally
- Treats `start_date` as "time zero": bars before it feed the pivot builder's lookback window (3 months), bars from `start_date` onward are fed to the pipeline one by one in time order
- Parallelises across multiple test runs if needed (e.g. testing multiple date ranges)
- Routes orders to `test_api` instead of the real broker bus
- After completion, calls the report writer

### Pivot Builder (`src/pivot/`)
- Called every 30 bars (or on demand)
- Input: last 3 months of OHLCV bars (fetched from long-term DB or provided by batch runner)
- Output: `List[float]` — pivots sorted ascending, filtered to ±12% of last close
- Length is variable
- **Batch optimisation**: pivots can be pre-calculated for every 30-bar checkpoint and cached in `data/pivots_cache/` — batch runner injects the cached list rather than recomputing

### Analyst (`src/analyst/`)
- Stateless function: `analyse(bar: OHLCVBar, status: MachineStatus, pivots: List[float], recent_bars: List[OHLCVBar], bookkeeper: Bookkeeper, recalc_hook: Callable[[], List[float]] | None = None) -> List[AnalystOrder]`
- Contains all trading logic (see `analyst_logic.md`)
- Returns a list of `AnalystOrder` instructions; never communicates directly with broker
- **Entry + stop-loss flow**: on every IDLE bar where VWAP and close agree on the same pivot, the analyst emits a `BUY_LIMIT` paired with a `SELL_STOP` (stop-loss) in the same response. The stop-loss fires only after the buy fills (`condition="on_fill:<uuid>"`). Once LONG, the analyst advances a watermark each time close breaks above the next pivot, emitting `UPDATE_STOPLOSS` to trail the stop-loss upward — locking in gains without ever moving it down.

### Trader (`src/trader/`)
- Stateless function: `process(orders: List[AnalystOrder], status: MachineStatus, bookkeeper: Bookkeeper) -> List[BrokerOrder]`
- Translates analyst instructions into structured broker orders
- Handles sizing: each order is for the full available cash (cash - min_cash_reserve)
- Does not execute — passes `BrokerOrder` objects to the broker bus

### Bookkeeper (`src/bookkeeper/`)
- Stateful: owns `cash`, `shares_held`, `ledger: List[LedgerEntry]`
- Receives `ExecutionConfirmation` from the broker bus
- Updates cash and shares on each confirmation
- Deduplicates confirmations (idempotency check on confirmation ID)
- Periodically reconciles cash and shares against broker API (streaming mode only)
- Exposes: `available_cash() -> float`

### Broker Bus (`src/bus/`)
- Single interface, two implementations:
  - `LiveBus`: wraps real broker API, handles retries, rate limits, errors
  - `TestBus` (in `dev_tools/test_api/`): simulates execution by checking future bars
- Both implement the same `BrokerBusProtocol` (Python `Protocol` class)

### Test API (`dev_tools/test_api/`)
- Receives a `BrokerOrder` and a view of future bars
- **Buy limit** (`LIMIT` / `BUY`): executes on the first future bar where `low <= limit_price`
- **Sell limit** (`LIMIT` / `SELL`): executes on the first future bar where `high >= limit_price`
- **Stop-loss** (`STOP_LIMIT` / `SELL`): executes on the first future bar where `low <= limit_price` — triggers when price drops *to or below* the stop level
- **Market order**: executes at next bar's open
- Returns an `ExecutionConfirmation` (or `None` if never filled within the test window)

### Scriber (`src/scriber/`)
- Streaming mode only
- Writes each incoming `OHLCVBar` to the long-term OHLCV DB
- Append-only; deduplicates on `(ticker, timestamp)`

### Report Writer (`dev_tools/report/`)
- Takes the bookkeeper's `ledger` and the full bar sequence
- Outputs a human-readable text file: one line per minute, showing bar data, machine status, orders issued, executions
- Saved to `outputs/active/YYYYMMDD_HHMMSS_report.txt`

---

## Data Flow — Streaming Mode (per bar)

```
Live feed
  → stream_entry receives OHLCVBar
  → scriber writes bar to DB
  → if bar_count % 30 == 0: pivot_builder recalculates pivots
  → analyst(bar, machine_status, pivots) → AnalystOrders
  → trader(AnalystOrders, machine_status) → BrokerOrders
  → live_bus sends BrokerOrders to broker API
  → broker API sends ExecutionConfirmation (async)
  → bookkeeper updates ledger, cash, shares
```

## Data Flow — Batch Mode (per test run)

```
batch_runner(ticker, start_date, end_date)
  → slices long-term DB: lookback window + test window
  → [optional] pre-calculates pivot snapshots every 30 bars → pivots_cache
  → for each bar in test window (sequential, simulating time):
      → if checkpoint: inject cached pivots (or recompute)
      → analyst(bar, machine_status, pivots) → AnalystOrders
      → trader(AnalystOrders, machine_status) → BrokerOrders
      → test_bus(BrokerOrders, future_bars) → ExecutionConfirmations
      → bookkeeper updates ledger, cash, shares
  → report_writer(ledger, all_bars) → outputs/active/report.txt
```

---

## TradingMachine Status

The `MachineStatus` dataclass is the single source of runtime state, passed explicitly:

```python
@dataclass
class MachineStatus:
    position: Literal["IDLE", "LONG"]
    watermark_level: int | None      # index into current pivots list
    initial_nav: float               # set at session start, used for daily loss check
    session_date: date               # to detect stop_trading_time crossing
    pending_orders: Dict[str, Literal["BUY", "SELL"]]  # order_id → side; {} when no open orders
    bar_count: int                   # total bars processed this session (for pivot timing)
```

---

## Key Constraints

- **One position at a time**: the machine is either IDLE or LONG, never partially invested
- **Full cash deployment**: each buy order uses all available cash minus `MIN_CASH_RESERVE`
- **Dual-mode parity**: the analyst, trader, and bookkeeper are identical in both modes — only the entry point and broker bus differ
- **Pivot list is immutable within a 30-bar window**: the analyst always works with the last computed pivot list
