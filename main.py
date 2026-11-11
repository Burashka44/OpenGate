"""
Точка входа VPN Telegram бота.

Инициализирует бота, диспетчер, применяет миграции и запускает polling.
"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, validate_runtime_config
from database.migrations import run_migrations

from bot.services.vpn_api import close_all_clients
from bot.services.scheduler import run_daily_tasks, run_update_check_scheduler, run_traffic_sync_scheduler
from bot.services.ops import healthcheck_loop, auto_renew_loop
from web.server import start_web_server, stop_web_server

# Импорт роутеров
from bot.handlers.user import router as user_router
from bot.handlers.admin import admin_router

validate_runtime_config()


# Создаём папку для логов если её нет (важно сделать до basicConfig)
os.makedirs("logs", exist_ok=True)


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            "logs/bot.log", 
            maxBytes=1024 * 1024,  # 1 мегабайт
            backupCount=3, 
            encoding="utf-8"
        )
    ]
)

# Уменьшаем шум от aiohttp
logging.getLogger("aiohttp").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)





async def on_startup(bot: Bot):
    """Действия при запуске бота."""
    logger.info("🚀 Бот запускается...")
    
    # Применяем миграции БД
    run_migrations()
    
    # Информация о боте
    bot_info = await bot.get_me()
    bot.my_username = bot_info.username
    logger.info(f"✅ Бот запущен: @{bot_info.username}")
    
    # Если обновления заблокированы — сразу уведомляем админов
    from bot.utils.update_block import is_update_blocked, get_blocked_message
    if is_update_blocked():
        from config import ADMIN_IDS
        from aiogram.types import InlineKeyboardButton
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        
        msg = get_blocked_message()
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✅ OK", callback_data="dismiss_msg"))
        kb = builder.as_markup()
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=msg,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить уведомление о блокировке админу {admin_id}: {e}")


async def on_shutdown(bot: Bot):
    """Действия при остановке бота."""
    logger.info("🛑 Бот останавливается...")

    try:
        from web.server import drain_background_tasks
        await drain_background_tasks(timeout=20.0)
    except Exception as e:
        logger.warning(f"drain_background_tasks: {e}")

    # Закрываем все VPN API сессии
    await close_all_clients()

    logger.info("✅ Бот остановлен")


async def main():
    """Главная функция запуска бота."""
    # Импортируем кастомную сессию с fallback для ошибок Markdown
    from bot.middlewares.parse_mode_fallback import SafeParseSession
    
    # Создаём бота с кастомной сессией и диспетчер
    session = SafeParseSession()
    bot = Bot(token=BOT_TOKEN, session=session)

    # Redis FSM при redis_fsm_url; MemoryStorage — только если не REQUIRE_REDIS_FSM
    storage = MemoryStorage()
    try:
        from database.requests import get_setting
        from config import REQUIRE_REDIS_FSM
        redis_url = (get_setting('redis_fsm_url', '') or '').strip()
        if redis_url:
            from aiogram.fsm.storage.redis import RedisStorage
            storage = RedisStorage.from_url(redis_url)
            logger.info("FSM storage: Redis")
        else:
            if REQUIRE_REDIS_FSM:
                raise SystemExit(
                    "REQUIRE_REDIS_FSM=1, но redis_fsm_url пуст. "
                    "Задайте URL в админке/settings или выключите флаг."
                )
            logger.warning("FSM storage: Memory (состояния теряются при рестарте)")
    except SystemExit:
        raise
    except Exception as e:
        from config import REQUIRE_REDIS_FSM
        if REQUIRE_REDIS_FSM:
            raise SystemExit(f"Redis FSM обязателен, но недоступен: {e}") from e
        logger.warning(f"Redis FSM недоступен, fallback MemoryStorage: {e}")
        storage = MemoryStorage()

    dp = Dispatcher(storage=storage)

    from bot.middlewares.rate_limit import RateLimitMiddleware
    from bot.middlewares.fsm_ttl import FsmTtlMiddleware
    dp.update.middleware(RateLimitMiddleware())
    dp.update.middleware(FsmTtlMiddleware())
    
    # Регистрируем роутеры
    # Порядок важен: сначала более специфичные, потом общие
    dp.include_router(admin_router)           # Админ-панель (общая)
    dp.include_router(user_router)            # Пользователь (имеет строгий внутренний порядок)
    
    # Глобальный обработчик ошибок сети / Telegram
    from aiogram.exceptions import (
        TelegramNetworkError,
        TelegramRetryAfter,
        TelegramForbiddenError,
        TelegramBadRequest,
    )
    from aiogram.types import ErrorEvent
    
    @dp.errors()
    async def global_error_handler(event: ErrorEvent):
        """Сетевые/API ошибки Telegram — без падения процесса."""
        exception = event.exception
        if isinstance(exception, TelegramRetryAfter):
            wait = getattr(exception, "retry_after", 5) or 5
            logger.warning(f"FloodWait {wait}s — жду")
            await asyncio.sleep(float(wait))
            return True
        if isinstance(exception, TelegramForbiddenError):
            logger.warning(f"Telegram Forbidden (бот заблокирован?): {exception}")
            return True
        if isinstance(exception, TelegramBadRequest):
            logger.warning(f"Telegram BadRequest: {exception}")
            return True
        if isinstance(exception, TelegramNetworkError):
            logger.warning(f"⚠️ Нет связи с Telegram API: {exception}")
            return True
        logger.error(f"Необработанная ошибка: {exception}", exc_info=True)
        return True
    
    # Регистрируем startup/shutdown
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Удаляем старые обновления и запускаем polling
    await bot.delete_webhook(drop_pending_updates=True)
    

    
    # Запускаем планировщик ежедневных задач (статистика + бэкапы)
    daily_tasks = asyncio.create_task(run_daily_tasks(bot))
    # Запускаем планировщик проверки обновлений
    update_tasks = asyncio.create_task(run_update_check_scheduler(bot))
    # Запускаем планировщик синхронизации трафика (каждые 5 мин)
    traffic_tasks = asyncio.create_task(run_traffic_sync_scheduler(bot))
    # Healthcheck панелей (каждые 5 мин, если включён)
    health_tasks = asyncio.create_task(healthcheck_loop(bot))
    # Автопродление с баланса (раз в час, если включено)
    renew_tasks = asyncio.create_task(auto_renew_loop(bot))
    
    # Встроенный веб-сервер (sub page + вебхуки), если web_enabled=1.
    # Миграции к этому моменту ещё не применены (on_startup вызывается в
    # start_polling), поэтому применяем здесь заранее — run_migrations идемпотентен.
    run_migrations()
    web_runner = await start_web_server(bot)

    async def _heartbeat_loop():
        from bot.services.vpn_api import prune_vpn_caches
        path = os.path.join("logs", "heartbeat")
        while True:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(str(asyncio.get_running_loop().time()))
                prune_vpn_caches()
            except Exception as e:
                logger.debug(f"heartbeat: {e}")
            await asyncio.sleep(30)

    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    
    try:
        await dp.start_polling(bot)
    finally:
        daily_tasks.cancel()
        update_tasks.cancel()
        traffic_tasks.cancel()
        health_tasks.cancel()
        renew_tasks.cancel()
        heartbeat_task.cancel()
        await stop_web_server(web_runner)
        await bot.session.close()


if __name__ == "__main__":
    # Запускаем бота
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки")
