"""secop_firstbid — scheduled first-bid submitter for SECOP II electronic reverse auctions.

Implements ``SECOP_FIRSTBID_PLAN.md`` (post-audit v2); each submodule names the plan
section it covers in its own docstring.

Nothing heavy is imported here: ``parse-page`` and the offline test suite must not pay
for ``requests``, and the fire-instant path must not pay for import work it does not use.
Callers import the submodule they need (``from secop_firstbid import endpoints``).
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
