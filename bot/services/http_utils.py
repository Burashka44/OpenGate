"""
Общие HTTP-таймауты и structured logging helpers.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

# Единый таймаут для panel HTTP-клиентов
DEFAULT_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=25)


def client_timeout(total: float = 30) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=total, connect=min(10, total), sock_read=max(5, total - 5))


def log_ctx(
    logger: logging.Logger,
    level: int,
    msg: str,
    *,
    order_id: Optional[str] = None,
    key_id: Optional[int] = None,
    server_id: Optional[int] = None,
    **extra: Any,
) -> None:
    """Структурированный лог с order_id / key_id / server_id."""
    parts = [msg]
    if order_id is not None:
        parts.append(f"order_id={order_id}")
    if key_id is not None:
        parts.append(f"key_id={key_id}")
    if server_id is not None:
        parts.append(f"server_id={server_id}")
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    logger.log(level, " | ".join(parts))
