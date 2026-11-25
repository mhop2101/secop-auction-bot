# CLAUDE.md

The instructions for working in this repository live in **[AGENTS.md](AGENTS.md)**. Read that file
first — it is the single source of truth for conventions, invariants, and the things you must not do.

It is kept as one file rather than two so the rules cannot drift apart.

Quick orientation while you're here:

- `docs/evidence.md` — what the source capture proves and what it doesn't. Read before changing
  `clock.py`, `bidder.py` or `schedule.py`; those three are the least constrained by evidence.
- `docs/open-questions.md` — the things nobody has fully observed that the design depends on.
- `SECOP_FIRSTBID_PLAN.md` — the specification. `PLAN_AUDIT.md` — the audit that corrected it.

Run the tests with `.venv/bin/python -m pytest -q`. They must pass offline.
