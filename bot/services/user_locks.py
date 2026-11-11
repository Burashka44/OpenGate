"""
Блокировки для синхронизации операций с балансом пользователей.

Используется для предотвращения race conditions при операциях с балансом.
Словарь периодически чистится от неиспользуемых lock'ов (защита от роста RAM).
"""
import asyncio
import time
from collections import defaultdict
from typing import Dict, Tuple


# user_id -> (Lock, last_used_monotonic)
_user_locks: Dict[int, Tuple[asyncio.Lock, float]] = {}
_meta_lock = asyncio.Lock()
_MAX_IDLE_SEC = 3600
_MAX_ENTRIES = 5000


class _LockProxy:
    """Прокси: async with user_locks[uid] как раньше."""

    def __getitem__(self, user_id: int) -> asyncio.Lock:
        now = time.monotonic()
        entry = _user_locks.get(user_id)
        if entry is None:
            lock = asyncio.Lock()
            _user_locks[user_id] = (lock, now)
            if len(_user_locks) > _MAX_ENTRIES:
                _evict_idle(now)
            return lock
        lock, _ = entry
        _user_locks[user_id] = (lock, now)
        return lock


def _evict_idle(now: float | None = None) -> None:
    now = now if now is not None else time.monotonic()
    stale = [
        uid for uid, (lock, ts) in _user_locks.items()
        if not lock.locked() and (now - ts) > _MAX_IDLE_SEC
    ]
    for uid in stale:
        _user_locks.pop(uid, None)
    # Если всё ещё переполнено — выкидываем самые старые незалоченные
    if len(_user_locks) > _MAX_ENTRIES:
        candidates = sorted(
            ((uid, ts) for uid, (lock, ts) in _user_locks.items() if not lock.locked()),
            key=lambda x: x[1],
        )
        for uid, _ in candidates[: max(0, len(_user_locks) - _MAX_ENTRIES)]:
            _user_locks.pop(uid, None)


user_locks = _LockProxy()
