\# Algo Trader — Project Memory



\## Architecture

@docs/architecture.md



\## Key Design Rules (always enforce)

\- Every component in src/ is a Python module with a clear interface

\- Dual entry points: `run\_stream(bar: OHLCVBar)` and `run\_batch(df: pd.DataFrame)`

\- Batch mode must use parallel execution for performance (e.g. concurrent.futures or joblib)

\- Pivot builder runs every 30 bars in batch mode (= every 30min in live)

\- NO shared mutable state between components — pass objects explicitly

\- Test\_API is only ever imported in test/batch contexts, never in streaming



\## Stack

\- Python 3.11+

\- pandas for batch data

\- SQLite for long-term OHLCV DB (src/scriber)

\- pytest for tests



\## Commands

\- `pytest tests/` — run all tests

\- `python src/entry/batch.py --input data/raw/sample.csv` — run batch

\- `python src/entry/stream.py` — run live



\## File naming

\- Source: snake\_case.py

\- Data files: YYYYMMDD\_ticker\_ohlcv.csv

\- Output reports: YYYYMMDD\_HHMMSS\_report.txt



\## Best practices

\- flag inconsistencies with # REVIEW: but don't resolve silently

