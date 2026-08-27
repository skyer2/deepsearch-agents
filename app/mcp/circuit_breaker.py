"""Per-server circuit breaker。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    state: str = "closed"  # closed | open | half_open


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 5, reset_sec: float = 30.0):
        self.failure_threshold = max(1, failure_threshold)
        self.reset_sec = max(1.0, reset_sec)
        self._states: dict[str, BreakerState] = {}
        self._lock = threading.Lock()

    def before_call(self, server_id: str) -> None:
        if not self.allow(server_id):
            raise CircuitOpenError(f"circuit_open:{server_id}")

    def allow(self, server_id: str) -> bool:
        now = time.time()
        with self._lock:
            state = self._states.setdefault(server_id, BreakerState())
            if state.state == "open":
                if now - state.opened_at >= self.reset_sec:
                    state.state = "half_open"
                    return True
                return False
            return True

    def record_success(self, server_id: str) -> None:
        with self._lock:
            self._states[server_id] = BreakerState()

    def record_failure(self, server_id: str) -> None:
        with self._lock:
            state = self._states.setdefault(server_id, BreakerState())
            state.failures += 1
            if state.failures >= self.failure_threshold or state.state == "half_open":
                state.state = "open"
                state.opened_at = time.time()


_breaker: CircuitBreaker | None = None


def get_mcp_circuit_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        try:
            from app.config.loader import get_harness_config

            cfg = get_harness_config()
            _breaker = CircuitBreaker(
                failure_threshold=int(getattr(cfg, "mcp_breaker_failure_threshold", 5) or 5),
                reset_sec=float(getattr(cfg, "mcp_breaker_reset_sec", 30) or 30),
            )
        except Exception:
            _breaker = CircuitBreaker()
    return _breaker


def reset_mcp_circuit_breaker() -> None:
    global _breaker
    _breaker = None
