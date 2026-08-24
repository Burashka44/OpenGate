"""
Админ-команды: операционные настройки (веб-сервер, обслуживание, алерты).

Команды:
    /ops — сводка текущих настроек
    /ops_set КЛЮЧ ЗНАЧЕНИЕ — изменить настройку (whitelist)
    /maintenance on|off — быстрый режим обслуживания
"""
import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject, StateFilter

from bot.utils.admin import is_admin
from bot.utils.text import escape_html, safe_edit_or_send
from database.requests import get_setting, set_setting

logger = logging.getLogger(__name__)

router = Router()

# Ключ → (описание, тип: bool|int|str)
OPS_SETTINGS = {
    'web_enabled':                  ('Веб-сервер (sub page + вебхуки)', 'bool'),
    'web_port':                     ('Порт веб-сервера', 'int'),
    'web_public_url':               ('Публичный URL (https://sub.домен)', 'str'),
    'maintenance_mode':             ('Режим обслуживания (блок покупок)', 'bool'),
    'healthcheck_enabled':          ('Мониторинг панелей (каждые 5 мин)', 'bool'),
    'healthcheck_auto_maintenance': ('Авто-maintenance при падении всех панелей', 'bool'),
    'admin_alerts_chat_id':         ('Чат для алертов (пусто = админам в ЛС)', 'str'),
    'admin_alerts_payments':        ('Алерты о платежах', 'bool'),
    'admin_alerts_new_users':       ('Алерты о новых пользователях', 'bool'),
    'auto_renew_enabled':           ('Автопродление ключей с баланса', 'bool'),
    'trial_channel_id':             ('Канал для trial-гейта (@channel или -100…)', 'str'),
    'trial_channel_link':           ('Ссылка на канал для trial-гейта', 'str'),
    'link_reset_cooldown_hours':    ('Кулдаун сброса ссылки (часы)', 'int'),
}


def _format_value(key: str, value: str, vtype: str) -> str:
    if vtype == 'bool':
        return '🟢 вкл' if value == '1' else '⚪ выкл'
    if not value:
        return '<i>не задано</i>'
    return f'<code>{escape_html(value)}</code>'


@router.message(Command('ops'), StateFilter('*'))
async def cmd_ops(message: Message):
    """Сводка операционных настроек."""
    if not is_admin(message.from_user.id):
        return

    lines = ["🛠️ <b>Операционные настройки</b>\n"]
    for key, (desc, vtype) in OPS_SETTINGS.items():
        value = get_setting(key, '') or ''
        lines.append(f"▪️ {desc}\n   <code>{key}</code> = {_format_value(key, value, vtype)}")

    lines.append(
        "\n<b>Изменить:</b> <code>/ops_set КЛЮЧ ЗНАЧЕНИЕ</code>\n"
        "Для bool-настроек: 1 = вкл, 0 = выкл.\n"
        "Очистить строку: <code>/ops_set КЛЮЧ -</code>\n\n"
        "<b>Быстро:</b> /maintenance on | /maintenance off"
    )
    await safe_edit_or_send(message, '\n'.join(lines), force_new=True)


@router.message(Command('ops_set'), StateFilter('*'))
async def cmd_ops_set(message: Message, command: CommandObject):
    """Изменяет операционную настройку из whitelist."""
    if not is_admin(message.from_user.id):
        return

    args = (command.args or '').split(maxsplit=1)
    if len(args) < 2:
        await safe_edit_or_send(
            message,
            "📖 <b>Формат:</b> <code>/ops_set КЛЮЧ ЗНАЧЕНИЕ</code>\n\nСписок ключей: /ops",
            force_new=True,
        )
        return

    key, value = args[0].strip(), args[1].strip()
    if key not in OPS_SETTINGS:
        await safe_edit_or_send(
            message,
            f"❌ Ключ <code>{escape_html(key)}</code> не поддерживается. Список: /ops",
            force_new=True,
        )
        return

    (desc, vtype) = OPS_SETTINGS[key]

    if value == '-':
        value = ''
    elif vtype == 'bool':
        if value not in ('0', '1'):
            await safe_edit_or_send(message, "❌ Для этой настройки допустимо только 1 (вкл) или 0 (выкл).", force_new=True)
            return
    elif vtype == 'int':
        if not value.isdigit():
            await safe_edit_or_send(message, "❌ Значение должно быть числом.", force_new=True)
            return
    elif key == 'web_public_url':
        value = value.rstrip('/')
        if value and not value.startswith(('http://', 'https://')):
            await safe_edit_or_send(message, "❌ URL должен начинаться с http:// или https://", force_new=True)
            return

    set_setting(key, value)
    logger.info(f"Admin {message.from_user.id} изменил {key} = {value!r}")

    note = ""
    if key in ('web_enabled', 'web_port'):
        note = "\n\n⚠️ Изменения веб-сервера вступят в силу после перезапуска бота."

    await safe_edit_or_send(
        message,
        f"✅ <b>{escape_html(desc)}</b>\n<code>{key}</code> = {_format_value(key, value, vtype)}{note}",
        force_new=True,
    )


@router.message(Command('maintenance'), StateFilter('*'))
async def cmd_maintenance(message: Message, command: CommandObject):
    """Быстрое включение/выключение режима обслуживания."""
    if not is_admin(message.from_user.id):
        return

    arg = (command.args or '').strip().lower()
    if arg not in ('on', 'off'):
        current = get_setting('maintenance_mode', '0') == '1'
        status = '🟠 включён' if current else '🟢 выключен'
        await safe_edit_or_send(
            message,
            f"🛠️ Режим обслуживания: <b>{status}</b>\n\n"
            "<code>/maintenance on</code> — включить (покупки будут заблокированы)\n"
            "<code>/maintenance off</code> — выключить",
            force_new=True,
        )
        return

    set_setting('maintenance_mode', '1' if arg == 'on' else '0')
    set_setting('maintenance_auto_set', 'manual' if arg == 'on' else '0')
    if arg == 'on':
        await safe_edit_or_send(
            message,
            "🟠 <b>Режим обслуживания включён.</b>\n\nПокупки и продления заблокированы, пользователи увидят предупреждение.",
            force_new=True,
        )
    else:
        await safe_edit_or_send(message, "🟢 <b>Режим обслуживания выключен.</b> Бот работает штатно.", force_new=True)
