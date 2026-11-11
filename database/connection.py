"""
Модуль подключения к базе данных SQLite.

Предоставляет контекстный менеджер для безопасной работы с БД
и async-обёртку, чтобы не блокировать event loop.
"""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional, TypeVar, Union

# Путь к файлу базы данных
DB_PATH = Path(__file__).parent / "vpn_bot.db"

_T = TypeVar("_T")
# Отдельный пул для sync SQLite из async-кода
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opengate-db")


def backup_database(
    dest_path: Union[str, Path],
    src_path: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Консистентный онлайн-бэкап SQLite (включая WAL) через Connection.backup().
    """
    src = Path(src_path) if src_path else DB_PATH
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source = sqlite3.connect(str(src), timeout=60)
    try:
        target = sqlite3.connect(str(dest), timeout=60)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


def get_connection() -> sqlite3.Connection:
    """
    Создаёт новое соединение с БД.
    
    Returns:
        sqlite3.Connection: Соединение с БД
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row  # Доступ к полям по имени
    conn.execute("PRAGMA foreign_keys = ON")  # Включаем FK
    # WAL: конкурентные чтения при записи (веб-сервер + polling + планировщики)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_db():
    """
    Контекстный менеджер для работы с БД.
    
    Автоматически делает commit при успехе и rollback при ошибке.
    
    Пример:
        with get_db() as conn:
            cursor = conn.execute("SELECT * FROM users")
            users = cursor.fetchall()
    
    Yields:
        sqlite3.Connection: Соединение с БД
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def run_db(fn: Callable[[], _T]) -> _T:
    """
    Выполняет синхронную DB-функцию вне event loop.

    Пример:
        users = await run_db(lambda: get_all_users())
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, fn)
