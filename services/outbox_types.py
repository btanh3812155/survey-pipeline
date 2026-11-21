"""Durable effect results; provider credentials never belong in these records.

Handlers await ``guard()`` before every provider operation. The worker also
supplies ``guard.record(receipt)`` to persist partial evidence before another
operation; a false return forbids new provider calls.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Outcome:
    outcome: str
    reason: str = ""
    receipt: Dict[str, Any] = field(default_factory=dict)
    retry_after: Optional[int] = None
