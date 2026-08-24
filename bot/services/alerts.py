"""
Алерты администраторам: платежи, новые пользователи, инциденты серверов.

Получатель — чат admin_alerts_chat_id, либо все ADMIN_IDS в ЛС.
"""
import logging
from typing import Optional, Dict, Any

from aiogram import Bot

from config import ADMIN_IDS
from database.requests import get_setting, get_admin_alerts_chat_id

logger = logging.getLogger(__name__)


async def send_admin_alert(bot: Bot, text: str) -> None:
    """Отправляет алерт в настроенный чат или всем админам в ЛС."""
    chat_id = get_admin_alerts_chat_id()
    targets = [chat_id] if chat_id else list(ADMIN_IDS)
    for target in targets:
        try:
            await bot.send_message(chat_id=target, text=text, parse_mode='HTML')
        except Exception as e:
            logger.warning(f"Алерт не доставлен в {target}: {e}")


def _format_amount(order: Dict[str, Any]) -> str:
    """Человекочитаемая сумма ордера."""
    ptype = order.get('payment_type') or '?'
    if ptype == 'stars' and order.get('amount_stars'):
        return f"⭐{order['amount_stars']}"
    if ptype == 'crypto' and order.get('amount_cents'):
        return f"${order['amount_cents'] / 100:g}"
    if order.get('amount_cents'):
        return f"{order['amount_cents'] / 100:g} ₽"
    return "—"


async def send_payment_alert(bot: Bot, order: Dict[str, Any]) -> None:
    """Алерт об успешном платеже (если admin_alerts_payments=1)."""
    if get_setting('admin_alerts_payments', '1') != '1':
        return
    purpose = "пополнение баланса" if order.get('purpose') == 'topup' else "покупка/продление"
    text = (
        f"💰 <b>Платёж</b> ({purpose})\n"
        f"Ордер: <code>{order.get('order_id', '?')}</code>\n"
        f"Метод: {order.get('payment_type', '?')}\n"
        f"Сумма: {_format_amount(order)}\n"
        f"User ID: {order.get('user_id', '?')}"
    )
    await send_admin_alert(bot, text)


async def send_new_user_alert(bot: Bot, telegram_id: int, username: Optional[str]) -> None:
    """Алерт о новом пользователе (если admin_alerts_new_users=1)."""
    if get_setting('admin_alerts_new_users', '0') != '1':
        return
    uname = f"@{username}" if username else "без username"
    await send_admin_alert(bot, f"👤 <b>Новый пользователь</b>: {uname} (<code>{telegram_id}</code>)")
