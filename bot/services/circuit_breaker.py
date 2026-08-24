"""
Circuit breaker по server_id (связка с healthcheck).
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# server_id -> (failures, open_until_ts)
_state: Dict[int, Tuple[int, float]] = {}

FAILURE_THRESHOLD = 3
OPEN_SECONDS = 300  # 5 минут


def record_success(server_id: int) -> None:
    _state.pop(server_id, None)


def record_failure(server_id: int) -> None:
    fails, _ = _state.get(server_id, (0, 0.0))
    fails += 1
    open_until = 0.0
    if fails >= FAILURE_THRESHOLD:
        open_until = time.time() + OPEN_SECONDS
        logger.warning(
            f"circuit breaker OPEN for server_id={server_id} "
            f"({fails} failures, {OPEN_SECONDS}s)"
        )
        fails = 0
    _state[server_id] = (fails, open_until)


def is_open(server_id: int) -> bool:
    fails, open_until = _state.get(server_id, (0, 0.0))
    if open_until and time.time() < open_until:
        return True
    if open_until and time.time() >= open_until:
        _state[server_id] = (0, 0.0)
    return False


def assert_closed(server_id: int) -> None:
    if is_open(server_id):
        from bot.services.panels.base import VPNAPIError
        raise VPNAPIError(f"Circuit breaker open for server {server_id}")
