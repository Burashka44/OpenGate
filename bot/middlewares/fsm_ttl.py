"""Сброс протухших FSM-состояний (залипание сценариев)."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import TelegramObject

try:
    from config import FSM_STATE_TTL_SECONDS
except Exception:
    FSM_STATE_TTL_SECONDS = 1800


class FsmTtlMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if state is None:
            return await handler(event, data)

        current = await state.get_state()
        if not current:
            return await handler(event, data)

        raw = await state.get_data()
        started = raw.get("_fsm_started_at")
        now = time.time()
        ttl = max(60, int(FSM_STATE_TTL_SECONDS or 1800))
        if started is None:
            await state.update_data(_fsm_started_at=now)
        elif now - float(started) > ttl:
            await state.clear()
            # Не глотаем апдейт молча: пользователь должен начать сценарий заново
            from aiogram.types import CallbackQuery, Update
            cb = event.callback_query if isinstance(event, Update) else (
                event if isinstance(event, CallbackQuery) else None
            )
            if cb is not None:
                try:
                    await cb.answer("⏱ Сессия истекла. Начните заново.", show_alert=True)
                except Exception:
                    pass
        return await handler(event, data)
