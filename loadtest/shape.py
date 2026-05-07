"""Phase 3 Locust load shape (roadmap §4 line 157, ADR-026).

Two phases:
  - warmup:   t in [0,  30) -> 50 users  (cache fill, JIT warm)
  - measured: t in [30, 90) -> 200 users (the comparison window)

Returns None at t >= 90, which signals locust to stop.

ADR-026: phase names "warmup"/"measured" are conventions; this module
hardcodes them per the MVP simplification (no test-cases.yaml).
"""

import os

from locust import LoadTestShape


WARMUP_DURATION_S = 30
MEASURED_DURATION_S = int(os.environ.get("MEASURED_DURATION_S", "60"))
TOTAL_DURATION_S = WARMUP_DURATION_S + MEASURED_DURATION_S
WARMUP_USERS = 50
MEASURED_USERS = 200
SPAWN_RATE = 50  # users/s — fast enough to reach the target user count quickly.


class ExperimentShape(LoadTestShape):
    """Two-phase shape: warmup 30s @ 50 users, measured 60s @ 200 users."""

    def tick(self) -> tuple[int, float] | None:
        """Return (user_count, spawn_rate) for the current second.

        Returning None tells locust to stop the test.
        """
        run_time: float = self.get_run_time()  # type: ignore[no-untyped-call]
        if run_time < WARMUP_DURATION_S:
            return (WARMUP_USERS, float(SPAWN_RATE))
        if run_time < TOTAL_DURATION_S:
            return (MEASURED_USERS, float(SPAWN_RATE))
        return None
