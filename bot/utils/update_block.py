"""
Управление блокировкой обновлений.

Когда блокирующее обновление установлено — обычные обновления
и автопроверка отключаются, пока не будут выполнены условия разблокировки.

Настройка в settings:
- update_blocked: '1' или '0' — флаг блокировки

Текст сообщения берётся из bot.blocking_update.BLOCKING_MESSAGE,
если не задан — используется DEFAULT_BLOCKED_MESSAGE.
"""
import logging
import importlib

from database.requests import get_setting, set_setting

logger = logging.getLogger(__name__)

DEFAULT_BLOCKED_MESSAGE = (
    "🔒 <b>Обновления приостановлены</b>\n\n"
    "Для продолжения автоматических обновлений "
    "необходимо выполнить определённые действия в боте.\n\n"
    "Доступные режимы обновления:\n"
    "• Команда /update — экстренное обновление\n"
    "• Принудительная перезапись в настройках\n\n"
    "После выполнения требуемых действий блокировка снимется автоматически."
)


def is_update_blocked() -> bool:
    return get_setting('update_blocked', '0') == '1'


def get_blocked_message() -> str:
    try:
        mod = importlib.import_module('bot.blocking_update')
        custom = getattr(mod, 'BLOCKING_MESSAGE', '')
        if custom:
            return custom
    except Exception:
        pass
    return DEFAULT_BLOCKED_MESSAGE


def set_update_blocked() -> None:
    set_setting('update_blocked', '1')
    logger.info("Блокировка обновлений установлена")


def clear_update_blocked() -> None:
    set_setting('update_blocked', '0')
    logger.info("Блокировка обновлений снята")


def try_unblock() -> bool:
    """
    Проверяет условия разблокировки через bot.blocking_update.

    Импортирует модуль, проверяет наличие check_unblock_conditions().
    Если функция есть и вернула True — снимает блокировку.

    Returns:
        True если блокировка была снята
    """
    if not is_update_blocked():
        return False

    try:
        mod = importlib.import_module('bot.blocking_update')
    except Exception as e:
        logger.debug(f"Модуль blocking_update не найден: {e}")
        return False

    check_fn = getattr(mod, 'check_unblock_conditions', None)
    if check_fn is None:
        logger.debug("Функция check_unblock_conditions не определена")
        return False

    try:
        result = check_fn()
        if result:
            logger.info("Условия разблокировки выполнены, снимаем блокировку")
            clear_update_blocked()
            return True
        else:
            logger.debug("Условия разблокировки НЕ выполнены")
            return False
    except Exception as e:
        logger.error(f"Ошибка в check_unblock_conditions: {e}")
        return False
