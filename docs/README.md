# Documentation

Suggested reading order, depending on why you're here.

**You want to place a bid.**
[evidence.md](evidence.md) → [safety.md](safety.md) → [operations.md](operations.md) →
[configuration.md](configuration.md)

Start with the evidence page even though it looks like background material. It is the list of things
this app cannot promise, and two of them (the audit-log footprint, and the fact that no bid has ever
actually been submitted through this code path) change how you'd run it.

**You're changing the code.**
[../AGENTS.md](../AGENTS.md) → [architecture.md](architecture.md) → [evidence.md](evidence.md) →
[open-questions.md](open-questions.md)

`AGENTS.md` has the invariants. Several of them look arbitrary until you read why, which is in the
other three.

**Something went wrong in a run.**
[operations.md](operations.md#reading-the-result) → [logging.md](logging.md) → [api.md](api.md)

**You're deciding whether the timing design is sound.**
[timing.md](timing.md), then the clock section of [evidence.md](evidence.md#clock-evidence).

---

| File | What's in it |
|---|---|
| [evidence.md](evidence.md) | What the source capture proves, what it doesn't, and the vocabulary for telling those apart |
| [architecture.md](architecture.md) | Module map, data flow, and the reasoning behind each design decision |
| [configuration.md](configuration.md) | Every config key, its default, and why |
| [operations.md](operations.md) | Cold start → live bid, plus how to read a run and troubleshoot it |
| [timing.md](timing.md) | The fire instant, the clock diagnostics, the wave schedule |
| [safety.md](safety.md) | Audit-log footprint, credentials, and the pre-run checklist |
| [api.md](api.md) | The SECOP II e-auction HTTP surface, marked by provenance |
| [logging.md](logging.md) | Run directory layout, event and summary schemas, redaction rules |
| [open-questions.md](open-questions.md) | The unknowns the design rests on, what 2026-11-20 showed, and how to settle each |

The specification is [`../SECOP_FIRSTBID_PLAN.md`](../SECOP_FIRSTBID_PLAN.md) and the audit that
corrected it is [`../PLAN_AUDIT.md`](../PLAN_AUDIT.md). Neither is documentation of the code — they are
the record of how the design was arrived at, and the audit in particular is worth reading for its
account of how a plausible inference ends up written in the voice of an observation.
