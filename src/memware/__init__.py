"""memware — memory for AI agents that only remembers the latest truth.

Two stores, one SQLite file:

* **turns** — immutable evidence: what was said in past sessions, indexed with
  FTS5 for cheap, model-free recall.
* **beliefs** — a bi-temporal ledger of facts. A new value for the same
  ``(subject, relation)`` key supersedes the old one; recall only ever returns
  the currently valid belief. History is kept for audit, never surfaced.
"""

from memware.ledger import Outcome, Policy, approve, assert_belief, current, history, reject
from memware.store import Store

__all__ = [
    "Outcome",
    "Policy",
    "Store",
    "approve",
    "assert_belief",
    "current",
    "history",
    "reject",
]
__version__ = "0.1.1"
