"""
Админ-команды: промокоды и UTM-кампании.

Промокоды:
    /promo_add CODE TYPE VALUE [max_uses] [per_user] [days_valid]
        TYPE: days | percent | balance | trial
        VALUE: дни / проценты / рубли / 0 (для trial)
    /promo_list — список с активациями
    /promo_del CODE — удалить
    /promo_off CODE, /promo_on CODE — выключить/включить

UTM-кампании:
    /ad_add CODE Название кампании
    /ad_list — воронка: регистрации → trial → оплатившие
    /ad_del CODE
"""
import logging
from datetime import datetime, timedelta

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject, StateFilter

from bot.utils.admin import is_admin
from bot.utils.text import escape_html, safe_edit_or_send
from database.requests import (
    create_promo_code, get_promo_by_code, get_all_promo_codes,
    set_promo_active, delete_promo_code,
    create_campaign, get_campaign_by_code, get_all_campaigns_with_stats,
    delete_campaign,
)

logger = logging.getLogger(__name__)

router = Router()

_PROMO_TYPE_LABELS = {
    'days': 'дни',
    'percent': 'скидка %',
    'balance': 'баланс ₽',
    'trial': 'сброс trial',
}

_PROMO_ADD_USAGE = (
    "📖 <b>Формат:</b>\n"
    "<code>/promo_add КОД ТИП ЗНАЧЕНИЕ [макс_активаций] [на_юзера] [дней_действия]</code>\n\n"
    "<b>Типы:</b>\n"
    "• <code>days</code> — добавить дни к активному ключу\n"
    "• <code>percent</code> — скидка % на следующую покупку\n"
    "• <code>balance</code> — пополнить баланс (значение в рублях)\n"
    "• <code>trial</code> — разблокировать пробную подписку (значение 0)\n\n"
    "<b>Примеры:</b>\n"
    "<code>/promo_add NEWYEAR days 7 100 1 30</code>\n"
    "— 7 дней, максимум 100 активаций, 1 на юзера, действует 30 дней\n"
    "<code>/promo_add SALE20 percent 20</code>\n"
    "— скидка 20%, без лимитов"
)


@router.message(Command('promo_add'), StateFilter('*'))
async def cmd_promo_add(message: Message, command: CommandObject):
    """Создаёт промокод."""
    if not is_admin(message.from_user.id):
        return

    args = (command.args or '').split()
    if len(args) < 3:
        await safe_edit_or_send(message, _PROMO_ADD_USAGE, force_new=True)
        return

    code = args[0].strip()
    promo_type = args[1].strip().lower()
    if promo_type not in _PROMO_TYPE_LABELS:
        await safe_edit_or_send(
            message,
            f"❌ Неверный тип <code>{escape_html(promo_type)}</code>.\n\n" + _PROMO_ADD_USAGE,
            force_new=True,
        )
        return

    try:
        value = int(args[2])
        max_uses = int(args[3]) if len(args) > 3 else 0
        per_user = int(args[4]) if len(args) > 4 else 1
        days_valid = int(args[5]) if len(args) > 5 else 0
    except ValueError:
        await safe_edit_or_send(message, "❌ Значения должны быть числами.\n\n" + _PROMO_ADD_USAGE, force_new=True)
        return

    if value < 0 or (promo_type != 'trial' and value == 0):
        await safe_edit_or_send(message, "❌ Значение должно быть больше 0.", force_new=True)
        return
    if promo_type == 'percent' and value > 99:
        await safe_edit_or_send(message, "❌ Скидка не может быть больше 99%.", force_new=True)
        return

    # balance хранится в копейках
    stored_value = value * 100 if promo_type == 'balance' else value

    expires_at = None
    if days_valid > 0:
        expires_at = (datetime.utcnow() + timedelta(days=days_valid)).strftime('%Y-%m-%d %H:%M:%S')

    promo_id = create_promo_code(code, promo_type, stored_value, max_uses, per_user, expires_at)
    if promo_id is None:
        await safe_edit_or_send(
            message,
            f"❌ Промокод <code>{escape_html(code)}</code> уже существует.",
            force_new=True,
        )
        return

    bot_info = await message.bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=promo_{code}"

    value_display = {
        'days': f"{value} дн.",
        'percent': f"{value}%",
        'balance': f"{value} ₽",
        'trial': "сброс trial",
    }[promo_type]

    await safe_edit_or_send(
        message,
        f"✅ Промокод создан!\n\n"
        f"🎟️ Код: <code>{escape_html(code)}</code>\n"
        f"🎁 Эффект: {_PROMO_TYPE_LABELS[promo_type]} — {value_display}\n"
        f"🔢 Лимит: {max_uses if max_uses else '∞'} всего, {per_user} на юзера\n"
        f"⏳ Действует: {f'{days_valid} дн.' if days_valid else 'бессрочно'}\n\n"
        f"🔗 Ссылка для рекламы:\n<code>{escape_html(deep_link)}</code>",
        force_new=True,
    )


@router.message(Command('promo_list'), StateFilter('*'))
async def cmd_promo_list(message: Message):
    """Список промокодов."""
    if not is_admin(message.from_user.id):
        return

    promos = get_all_promo_codes()
    if not promos:
        await safe_edit_or_send(
            message,
            "🎟️ Промокодов пока нет.\n\nСоздайте: <code>/promo_add КОД ТИП ЗНАЧЕНИЕ</code>",
            force_new=True,
        )
        return

    lines = ["🎟️ <b>Промокоды:</b>\n"]
    for p in promos[:50]:
        status = "🟢" if p['is_active'] else "⚪"
        value = p['value'] or 0
        if p['promo_type'] == 'balance':
            effect = f"+{value / 100:g} ₽"
        elif p['promo_type'] == 'days':
            effect = f"+{value} дн."
        elif p['promo_type'] == 'percent':
            effect = f"-{value}%"
        else:
            effect = "trial"
        limit = f"{p['used_count']}/{p['max_uses'] if p['max_uses'] else '∞'}"
        expires = ""
        if p['expires_at']:
            expires = f", до {str(p['expires_at'])[:10]}"
        lines.append(
            f"{status} <code>{escape_html(p['code'])}</code> — {effect} "
            f"({limit} активаций{expires})"
        )

    lines.append(
        "\n<i>/promo_del КОД — удалить, /promo_off КОД — выключить, /promo_on КОД — включить</i>"
    )
    await safe_edit_or_send(message, '\n'.join(lines), force_new=True)


async def _promo_toggle(message: Message, command: CommandObject, active: bool):
    """Общий код для /promo_on и /promo_off."""
    code = (command.args or '').strip()
    if not code:
        await safe_edit_or_send(message, "❌ Укажите код: <code>/promo_on КОД</code>", force_new=True)
        return

    promo = get_promo_by_code(code)
    if not promo:
        await safe_edit_or_send(message, f"❌ Промокод <code>{escape_html(code)}</code> не найден.", force_new=True)
        return

    set_promo_active(promo['id'], active)
    status = "включён 🟢" if active else "выключен ⚪"
    await safe_edit_or_send(message, f"✅ Промокод <code>{escape_html(promo['code'])}</code> {status}", force_new=True)


@router.message(Command('promo_on'), StateFilter('*'))
async def cmd_promo_on(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    await _promo_toggle(message, command, True)


@router.message(Command('promo_off'), StateFilter('*'))
async def cmd_promo_off(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    await _promo_toggle(message, command, False)


@router.message(Command('promo_del'), StateFilter('*'))
async def cmd_promo_del(message: Message, command: CommandObject):
    """Удаляет промокод."""
    if not is_admin(message.from_user.id):
        return

    code = (command.args or '').strip()
    if not code:
        await safe_edit_or_send(message, "❌ Укажите код: <code>/promo_del КОД</code>", force_new=True)
        return

    promo = get_promo_by_code(code)
    if not promo:
        await safe_edit_or_send(message, f"❌ Промокод <code>{escape_html(code)}</code> не найден.", force_new=True)
        return

    delete_promo_code(promo['id'])
    await safe_edit_or_send(
        message,
        f"🗑️ Промокод <code>{escape_html(promo['code'])}</code> удалён "
        f"(активаций было: {promo['used_count']}).",
        force_new=True,
    )


# ============================================================================
# UTM-КАМПАНИИ
# ============================================================================

@router.message(Command('ad_add'), StateFilter('*'))
async def cmd_ad_add(message: Message, command: CommandObject):
    """Создаёт UTM-кампанию: /ad_add CODE Название."""
    if not is_admin(message.from_user.id):
        return

    args = (command.args or '').split(maxsplit=1)
    if len(args) < 2:
        await safe_edit_or_send(
            message,
            "📖 <b>Формат:</b> <code>/ad_add КОД Название кампании</code>\n\n"
            "<b>Пример:</b> <code>/ad_add tgads1 Telegram Ads январь</code>\n\n"
            "Вы получите ссылку вида <code>t.me/бот?start=ad_КОД</code> — "
            "все перешедшие по ней будут привязаны к кампании.",
            force_new=True,
        )
        return

    code, name = args[0].strip(), args[1].strip()
    campaign_id = create_campaign(code, name)
    if campaign_id is None:
        await safe_edit_or_send(message, f"❌ Кампания <code>{escape_html(code)}</code> уже существует.", force_new=True)
        return

    bot_info = await message.bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=ad_{code}"

    await safe_edit_or_send(
        message,
        f"✅ Кампания создана!\n\n"
        f"📣 <b>{escape_html(name)}</b>\n"
        f"🔗 Ссылка для размещения:\n<code>{escape_html(deep_link)}</code>\n\n"
        f"Статистика: /ad_list",
        force_new=True,
    )


@router.message(Command('ad_list'), StateFilter('*'))
async def cmd_ad_list(message: Message):
    """Воронка по кампаниям."""
    if not is_admin(message.from_user.id):
        return

    campaigns = get_all_campaigns_with_stats()
    if not campaigns:
        await safe_edit_or_send(
            message,
            "📣 Кампаний пока нет.\n\nСоздайте: <code>/ad_add КОД Название</code>",
            force_new=True,
        )
        return

    bot_info = await message.bot.get_me()

    lines = ["📣 <b>Рекламные кампании:</b>\n"]
    for c in campaigns[:50]:
        regs = c['registrations'] or 0
        trials = c['trials'] or 0
        payers = c['payers'] or 0
        conv = f"{payers / regs * 100:.1f}%" if regs else "—"
        lines.append(
            f"▪️ <b>{escape_html(c['name'])}</b> (<code>{escape_html(c['code'])}</code>)\n"
            f"   👥 {regs} рег. → 🎁 {trials} trial → 💰 {payers} оплат (конверсия {conv})\n"
            f"   <code>https://t.me/{bot_info.username}?start=ad_{escape_html(c['code'])}</code>"
        )

    lines.append("\n<i>/ad_del КОД — удалить кампанию</i>")
    await safe_edit_or_send(message, '\n'.join(lines), force_new=True)


@router.message(Command('ad_del'), StateFilter('*'))
async def cmd_ad_del(message: Message, command: CommandObject):
    """Удаляет кампанию."""
    if not is_admin(message.from_user.id):
        return

    code = (command.args or '').strip()
    if not code:
        await safe_edit_or_send(message, "❌ Укажите код: <code>/ad_del КОД</code>", force_new=True)
        return

    campaign = get_campaign_by_code(code)
    if not campaign:
        await safe_edit_or_send(message, f"❌ Кампания <code>{escape_html(code)}</code> не найдена.", force_new=True)
        return

    delete_campaign(campaign['id'])
    await safe_edit_or_send(message, f"🗑️ Кампания <code>{escape_html(campaign['code'])}</code> удалена.", force_new=True)
