# First Claude Code Prompt — Phase 0 Bootstrap

Paste this as your first message in a Claude Code session, from the project root folder.

---

## Prompt

I am building an automated trading algorithm in Python. The full design is documented in the `docs/` folder.

Please start by reading:
- @docs/architecture.md
- @docs/data_model.md  
- @docs/analyst_logic.md
- @docs/todo.md

Then execute Phase 0 from `todo.md`:

1. Create the full folder structure exactly as described in `architecture.md`. Create a `.gitkeep` in any empty folders so they are tracked by git.

2. Create `src/models.py` containing all the dataclasses defined in `data_model.md`. Use Python `dataclasses`, `typing`, and `datetime` from the standard library only — no third-party dependencies for the models file.

3. Create `src/config.py` containing all the constants listed in `analyst_logic.md` under the Constants table. Use a simple module-level constants pattern (no class, no env file for now). Add a comment next to each constant explaining what it controls.

4. Create stub modules for every component listed in `architecture.md` — one file per component. Each stub should contain:
   - A module-level docstring describing the component's responsibility (copy from architecture.md)
   - The signature of the main public function(s) with type hints
   - A `raise NotImplementedError` body
   - No imports that don't exist yet

   Stubs to create:
   - `src/entry/stream_entry.py`
   - `src/pivot/pivot_calc.py`
   - `src/analyst/analyst.py`
   - `src/trader/trader.py`
   - `src/bookkeeper/bookkeeper.py`
   - `src/bus/protocol.py` (a Python `Protocol` class, not a stub function)
   - `src/scriber/scriber.py`
   - `src/scriber/db.py`
   - `dev_tools/fake_api/fake_bus.py`
   - `dev_tools/batch_runner/runner.py`
   - `dev_tools/batch_runner/pivot_cache.py`
   - `dev_tools/report/writer.py`

5. Create `requirements.txt` with: `pandas`, `pytest`. No other dependencies for now.

6. Create a `tests/` folder with an empty `conftest.py`.

7. After creating all files, verify everything imports cleanly by running:
   ```
   python -c "from src import models, config; print('OK')"
   ```

8. Check off the Phase 0 items in `docs/todo.md`.

Do not implement any logic yet — stubs only. If you notice any inconsistency between the docs (e.g. a function signature that doesn't match the described data flow), flag it as a comment in the stub with `# REVIEW:` and continue. Do not resolve ambiguities silently.

---

## Before running this prompt — checklist

- [ ] `docs/architecture.md` is in place
- [ ] `docs/analyst_logic.md` is in place
- [ ] `docs/data_model.md` is in place
- [ ] `docs/todo.md` is in place
- [ ] `CLAUDE.md` is in place at project root
- [ ] You are running Claude Code from the project root directory
- [ ] Your existing pivot builder code is ready to paste in Phase 2

---

## What to do after Phase 0 completes

Open a **new Claude Code session** (`/clear`) and start Phase 1:

> "Read @docs/architecture.md and @docs/data_model.md and @docs/todo.md.
> Phase 0 is complete. Implement Phase 1 — the long-term OHLCV DB and Scriber.
> Follow the todo.md checklist item by item and check off each item when done."

One phase per session. Use `/clear` between phases.
