# Build Checklist

Managed by Claude Code. Check off items as they are completed.
Do not reorder items — dependencies flow top to bottom.

---

## Phase 0 — Scaffold

- [x] Create full folder structure (`src/`, `dev_tools/`, `docs/`, `data/`, `outputs/`, `experiments/`, `tests/`)
- [x] Create `src/models.py` with all dataclasses from `data_model.md`
- [x] Create `src/config.py` with all constants from `analyst_logic.md`
- [x] Create empty module stubs with docstrings for every component in `src/` and `dev_tools/`
- [x] Create `requirements.txt` with initial dependencies (pandas, pytest, sqlite3 stdlib)
- [x] Verify all stubs import cleanly with `python -c "import src.<module>"`

---

## Phase 1 — Long-term OHLCV DB + Scriber

- [x] Design and create SQLite schema for `bars` table (see `data_model.md`)
- [x] Implement `src/scriber/scriber.py`: `write_bar(bar: OHLCVBar) -> None` — append with dedup
- [x] Implement `src/scriber/db.py`: `fetch_bars(ticker, from_dt, to_dt) -> List[OHLCVBar]`
- [x] Write unit tests: `tests/test_scriber.py`
  - [x] Test deduplication (inserting same bar twice)
  - [x] Test range fetch returns correct bars in order

---

## Phase 2 — Pivot Builder

- [x] Copy in existing pivot calculation code → `src/pivot/pivot_calc.py`
- [x] Wrap with clean interface: `build_pivots(bars: List[OHLCVBar]) -> List[float]`
- [x] Implement pivot cache write/read: `dev_tools/batch_runner/pivot_cache.py`
  - [x] `save_cache(ticker, start, end, checkpoints: Dict[int, List[float]]) -> None`
  - [x] `load_cache(ticker, start, end) -> Dict[int, List[float]] | None`
- [x] Write unit tests: `tests/test_pivot_builder.py`
  - [x] Test output is sorted ascending
  - [x] Test output is filtered to ±12% of last close
  - [x] Test cache round-trip

---

## Phase 3 — Test API + Bookkeeper

- [x] Implement `dev_tools/test_api/test_bus.py`: `TestBus` implementing `BrokerBusProtocol`
  - [x] Buy limit: fills on first future bar where `low <= limit_price`
  - [x] Sell limit: fills on first future bar where `high >= limit_price`
  - [x] Market: fills at next bar's open
  - [x] Returns `None` if never filled within window
- [x] Define `src/bus/protocol.py`: `BrokerBusProtocol` (Python `Protocol` class)
- [x] Implement `src/bookkeeper/bookkeeper.py`
  - [x] `receive_confirmation(conf: ExecutionConfirmation) -> None` (with dedup)
  - [x] `available_cash() -> float`
  - [x] `shares_held() -> float`
  - [x] `get_ledger() -> List[LedgerEntry]`
- [x] Write unit tests: `tests/test_test_api.py`
  - [x] Test buy limit fills correctly
  - [x] Test sell limit fills correctly
  - [x] Test market order fills at next open
  - [x] Test order that never fills returns None
- [x] Write unit tests: `tests/test_bookkeeper.py`
  - [x] Test cash decreases on buy confirmation
  - [x] Test shares increase on buy confirmation
  - [x] Test deduplication of confirmation IDs
  - [x] Test available_cash respects MIN_CASH_RESERVE

---

## Phase 4 — Analyst

- [x] Implement `src/analyst/analyst.py`
  - [x] `analyse(bar, status, pivots, recent_bars, bookkeeper, recalc_hook) -> List[AnalystOrder]`
  - [x] Stop conditions (daily loss, post-hours idle)
  - [x] IDLE branch: cancel buys, VWAP + close pivot check, entry orders
  - [x] LONG branch: watermark advance, end-of-day market sell
- [x] Write unit tests: `tests/test_analyst.py`
  - [x] Test stop condition — daily loss triggers blank return
  - [x] Test stop condition — post-hours idle triggers blank return
  - [x] Test IDLE: pivot_vwap != pivot_close → no entry orders
  - [x] Test IDLE: pivot_vwap == pivot_close → buy limit + stop-loss emitted
  - [x] Test IDLE: i == 0 (no pivot below) → no orders, warning logged
  - [x] Test LONG: close above next pivot → watermark advances, stop-loss updated
  - [x] Test LONG: post stop_trading_time → market sell emitted

---

## Phase 5 — Trader

- [ ] Implement `src/trader/trader.py`
  - [ ] `process(orders: List[AnalystOrder], status: MachineStatus, bookkeeper) -> List[BrokerOrder]`
  - [ ] Translates each AnalystOrder type to a BrokerOrder
  - [ ] Computes share quantity from available cash and limit price
- [ ] Write unit tests: `tests/test_trader.py`
  - [ ] Test full cash deployment (quantity = available_cash / price)
  - [ ] Test conditional order passes condition string through
  - [ ] Test CANCEL_ALL_BUYS produces correct broker instruction

---

## Phase 6 — Batch Runner

- [ ] Implement `dev_tools/batch_runner/runner.py`
  - [ ] `run_batch(ticker, start_date, end_date) -> List[LedgerEntry]`
  - [ ] Slices DB: fetches 3-month lookback before start_date for pivot warm-up
  - [ ] Pre-calculates pivot cache for all 30-bar checkpoints in the test window
  - [ ] Replays test window bar by bar, maintaining MachineStatus
  - [ ] Routes orders to TestBus
  - [ ] Calls report writer on completion
- [ ] Write integration test: `tests/test_batch_runner.py`
  - [ ] Test with a small synthetic dataset (known pivots, known expected trades)

---

## Phase 7 — Report Writer

- [ ] Implement `dev_tools/report/writer.py`
  - [ ] `write_report(ledger, bars, output_path) -> None`
  - [ ] One line per bar: timestamp, OHLCV, machine status, orders issued, executions
  - [ ] Summary section: total trades, final NAV, max drawdown

---

## Phase 8 — Streaming Entry Point

- [ ] Implement `src/entry/stream_entry.py`
  - [ ] `on_bar(bar: OHLCVBar) -> None` — main callback for live feed
  - [ ] Calls scriber, manages pivot refresh counter, calls analyst → trader → live bus
- [ ] Stub `src/bus/live_bus.py` (interface only — real broker API integration is out of scope for now)

---

## Phase 9 — Integration & Validation

- [ ] Run batch on a real historical dataset (e.g. 1 Feb 2026 → 15 Mar 2026)
- [ ] Inspect report output manually
- [ ] Validate: no trades without a stop-loss
- [ ] Validate: cash never goes below MIN_CASH_RESERVE
- [ ] Validate: no duplicate ledger entries
- [ ] Validate: pivot list always sorted, always within ±12% range

---

## Deferred / Out of Scope for Now

- [ ] Real broker API integration (`src/bus/live_bus.py`)
- [ ] Additional stop conditions (see `analyst_logic.md`)
- [ ] Re-entry logic after stop-loss hit same day
- [ ] Partial fill handling
- [ ] Multi-ticker support
- [ ] Virtual pivot fallback for entry at index 0 (it does not handle the case where the pivots jumps below by more than 1.5%)
