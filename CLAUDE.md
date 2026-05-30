# Algo Trader — Project Memory

## Architecture & System Overview
- Core Reference: @docs/architecture.md
- Tech Stack: Python 3.11+, pandas (batch analysis), SQLite (OHLCV Database at `src/scriber`), pytest.

## Critical Build & Execution Commands
Always execute Python and testing suites using the explicit virtual environment paths below:

- **Install Dependencies:** `.venv\Scripts\pip.exe install -r requirements.txt`
- **Run Test Suite:** `.venv\Scripts\pytest` (or individual target: `.venv\Scripts\pytest tests/`)
- **Execute Batch Mode:** `.venv\Scripts\python.exe src/entry/batch.py --input data/raw/sample.csv`
- **Execute Live Streaming:** `.venv\Scripts\python.exe src/entry/stream.py`

## Architectural Design Rules (Strict Compliance Required)
- **Modular Isolation:** Every component inside `src/` must be a self-contained Python module displaying a clear, immutable interface. 
- **Dual Processing Architecture:** Components must support exactly two deterministic entry points: `run_stream(bar: OHLCVBar)` and `run_batch(df: pd.DataFrame)`.
- **Parallel Batch Execution:** Batch modes must execute in parallel to ensure optimal backtesting throughput (utilize `concurrent.futures` or `joblib`).
- **Pivot Alignment:** The Pivot Builder mechanism must run strictly every 30 bars in batch mode (equivalent to a 30-minute window in a live session).
- **Zero Mutable Shared State:** Do not share mutable states between components. All communication objects must be passed explicitly across clean interface boundaries to maintain thread safety.
- **Context Isolation:** `Test_API` must only be imported or utilized inside test/batch contexts. It is strictly banned from entering live streaming workflows.

## Development & Naming Conventions
- **Source Code Files:** `snake_case.py`
- **Data Collections:** `YYYYMMDD_ticker_ohlcv.csv`
- **Output Report Artifacts:** `YYYYMMDD_HHMMSS_report.txt`
- **Code Reviews:** Code anomalies or architectural inconsistencies should be explicitly flagged inline using a `# REVIEW:` comment block. Do not quietly or silently attempt to fix surrounding codebase issues outside your requested scope.

## Token Management & Guardrails
- **Ask Before Multi-Step Tasks:** Before executing any multi-step workflow, debugging loop, or systemic refactoring, present the proposed plan first and await explicit user approval.
- **Budget Notification:** If a requested task requires analyzing large numbers of files or deeply nested dependencies that will incur high token usage, warn the user with an estimated scale before proceeding.
- **Command Constraints:** Never execute long-running background processes or web-scraping commands without asking first.
- **No Speculative Coding:** Do not proactively fix related issues or refactor surrounding code unless specifically asked to do so by the user. Keep modifications scoped tightly to the explicit request.