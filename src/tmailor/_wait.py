"""Polling schedule for ``wait_for_message`` (shared by the sync and async mailboxes)."""

from __future__ import annotations

from typing import Optional

from ._core import MIN_RETRY_AFTER, RateLimitInfo

FIRST_POLL = 1.0
GROWTH = 1.5
MAX_POLL = 10.0
MIN_POLL = 0.5
# Keep this many requests of the current minute for the rest of your program.
RESERVE = 2
# Review 17e SDK-01: every HTTP request of the wait is capped by the time left before the deadline, so the last
# poll starts at least this many seconds before it (a poll exactly at the deadline would have no time to run).
LAST_POLL_RESERVE = 1.0


class WaitPlan:
    """Decides how long to sleep between two polls.

    * Without ``poll_interval`` the delay starts at 1 s and grows by 1.5x up to 10 s.
    * With ``poll_interval`` the delay is fixed (at least 0.5 s).
    * Rate limits: when ``X-RateLimit-Remaining`` is at or below 2 the poll waits for ``X-RateLimit-Reset``;
      when it is low it spreads the remaining requests until the reset.
    * Never sleeps past the deadline. Review 17e SDK-01: the last poll starts at least LAST_POLL_RESERVE (1 s)
      before the deadline, and every request is capped by the time left, so the whole wait ends by the deadline.
    """

    def __init__(self, timeout: float, poll_interval: Optional[float], now: float) -> None:
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        if poll_interval is not None and poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        self.deadline = now + float(timeout)
        self.fixed = poll_interval is not None
        self.interval = max(MIN_POLL, float(poll_interval)) if poll_interval is not None else FIRST_POLL

    def remaining(self, now: float) -> float:
        return self.deadline - now

    def next_delay(self, now: float, wall_now: float, rl: Optional[RateLimitInfo]) -> Optional[float]:
        """Seconds to sleep before the next poll, or None when less than LAST_POLL_RESERVE is left."""
        left = self.deadline - now - LAST_POLL_RESERVE  # Review 17e SDK-01: time left to sleep
        if left <= 0:
            return None
        delay = self.interval
        if not self.fixed:
            self.interval = min(self.interval * GROWTH, MAX_POLL)
        if rl is not None and rl.remaining is not None and rl.reset is not None:
            until_reset = max(0.0, rl.reset - wall_now) + 0.5
            if rl.remaining <= RESERVE:
                delay = max(delay, min(until_reset, 61.0))
            elif rl.limit and rl.remaining < max(5, rl.limit // 5):
                delay = max(delay, min(until_reset / max(1, rl.remaining - RESERVE), 61.0))
        return min(delay, left)

    def rate_limit_delay(self, now: float, retry_after: Optional[int]) -> Optional[float]:
        """Sleep after a 429 inside the wait loop, or None when it does not fit before the deadline."""
        left = self.deadline - now - LAST_POLL_RESERVE  # Review 17e SDK-01: keep time for the poll that follows
        # Review 17d SDK-01: Retry-After <= 0 still waits MIN_RETRY_AFTER (no busy loop, time always advances).
        wait = max(float(retry_after), MIN_RETRY_AFTER) if retry_after is not None else self.interval
        if wait > left:
            return None
        return wait
