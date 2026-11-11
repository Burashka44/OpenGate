"""Rate-limit middleware по config.RATE_LIMITS.

Вешается на Update — достаём CallbackQuery/Message из Update.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Deque, Dict, Optional, Tuple

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from config import ADMIN_IDS, RATE_LIMITS

# Только финальные/дорогие действия, не выбор метода оплаты в меню.
_CRITICAL_PREFIXES = (
    "topup_check:",
    "topup_pay:",
    "check_cryptobot:",
    "check_heleket:",
    "check_crypto:",
    "check_wata:",
    "check_platega:",
    "check_cardlink:",
    "pay_with_balance",
    "balance_pay",
    "trial_confirm",
    "promo_apply",
    "admin_server_add_save",
    "admin_update_bot_confirm",
    "admin_force_overwrite_confirm",
)


def _unwrap(event: TelegramObject) -> Tuple[Optional[CallbackQuery], Optional[Message], Optional[str]]:
    cb: Optional[CallbackQuery] = None
    msg: Optional[Message] = None
    if isinstance(event, Update):
        cb = event.callback_query
        msg = event.message or (cb.message if cb else None)  # type: ignore[assignment]
        if cb is None and event.edited_message:
            msg = event.edited_message
    elif isinstance(event, CallbackQuery):
        cb = event
        msg = event.message  # type: ignore[assignment]
    elif isinstance(event, Message):
        msg = event
    data_str = (cb.data if cb and cb.data else None) or (msg.text if msg and msg.text else None)
    return cb, msg, data_str


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._hits: Dict[int, Deque[float]] = defaultdict(deque)
        self._critical: Dict[int, Deque[float]] = defaultdict(deque)
        self._cmd_limit = int(RATE_LIMITS.get("commands_per_minute", 30))
        self._crit_limit = int(RATE_LIMITS.get("critical_operations_per_minute", 5))

    def _prune(self, q: Deque[float], now: float) -> None:
        while q and now - q[0] > 60.0:
            q.popleft()

    def _is_critical(self, data_str: Optional[str]) -> bool:
        if not data_str:
            return False
        low = data_str.strip().lower()
        return any(low.startswith(p) for p in _CRITICAL_PREFIXES)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        uid = user.id
        if uid in ADMIN_IDS:
            return await handler(event, data)

        cb, msg, data_str = _unwrap(event)
        now = time.monotonic()
        critical = self._is_critical(data_str)
        bucket = self._critical if critical else self._hits
        limit = self._crit_limit if critical else self._cmd_limit
        q = bucket[uid]
        self._prune(q, now)
        if len(q) >= limit:
            if cb is not None:
                try:
                    await cb.answer("⏳ Слишком много запросов. Подождите минуту.", show_alert=True)
                except Exception:
                    pass
            elif msg is not None:
                try:
                    await msg.answer("⏳ Слишком много запросов. Подождите минуту.")
                except Exception:
                    pass
            return None
        q.append(now)
        return await handler(event, data)
