"""Which failures are worth retrying, and how long to wait before the next attempt.

Two rules, both from docs/RELIABILITY.md:

1. Do not retry everything. A failure is retried only when the exception says it is transient, by
   carrying `retryable = True` (a provider outage, a rate limit, a timeout). A bug, a bad
   instruction or a refused permission fails at once: running it again cannot change the answer
   and costs time and money. An exception that says nothing is NOT retried.
2. Wait longer each time, with randomness. Many tasks hitting one struggling server and all
   retrying at the same instant keep it down. The delay doubles per attempt up to a cap
   ("exponential backoff"), and half of it is random ("jitter") so retries spread out.
"""

import random
from dataclasses import dataclass

from core.config import settings


class TaskTimeout(Exception):
    """A task attempt ran longer than its agent's timeout. Worth another attempt."""

    retryable = True


def is_retryable(error: BaseException) -> bool:
    return getattr(error, "retryable", False) is True


@dataclass(frozen=True)
class Backoff:
    """delay(n) for the n-th failed attempt: min(cap, base * 2**(n-1)), half fixed + half random.

    "Equal jitter": the fixed half guarantees that a retry really is delayed (full jitter can
    return about zero), the random half spreads simultaneous retries apart.
    """

    base: float
    cap: float

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        if attempt < 1:
            raise ValueError("attempt numbers start at 1")
        ceiling = min(self.cap, self.base * 2 ** (attempt - 1))
        return ceiling / 2 + (rng or random).uniform(0, ceiling / 2)

    @classmethod
    def from_settings(cls) -> "Backoff":
        return cls(settings.retry_base_seconds, settings.retry_max_seconds)
