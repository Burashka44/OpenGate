"""
Пополнение личного баланса (purpose='topup').

Поток: /topup или кнопка → выбор суммы → выбор способа (CryptoBot/Heleket)
→ инвойс с QR → «✅ Я оплатил» (или вебхук зачисляет мгновенно).

Баланс используется для оплаты тарифов и автопродления.
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.utils.text import escape_html, safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()

TOPUP_AMOUNTS_RUB = (100, 300, 500, 1000)


def _fmt_rub(cents: int) -> str:
    if cents % 100 == 0:
        return f'{cents // 100} ₽'
    return f'{cents / 100:.2f} ₽'.replace('.', ',')


def _topup_amounts_kb():
    builder = InlineKeyboardBuilder()
    row = [
        InlineKeyboardButton(text=f"{amount} ₽", callback_data=f"topup_amt:{amount}")
        for amount in TOPUP_AMOUNTS_RUB
    ]
    builder.row(*row[:2])
    builder.row(*row[2:])
    builder.row(InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
    return builder.as_markup()


def _topup_methods_kb(amount_rub: int, cryptobot: bool, heleket: bool):
    builder = InlineKeyboardBuilder()
    if cryptobot:
        builder.row(InlineKeyboardButton(
            text="🤖 CryptoBot (USDT/TON)",
            callback_data=f"topup_pay:cryptobot:{amount_rub}"
        ))
    if heleket:
        builder.row(InlineKeyboardButton(
            text="🧿 Heleket (крипта)",
            callback_data=f"topup_pay:heleket:{amount_rub}"
        ))
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="topup_menu"),
        InlineKeyboardButton(text="🈴 На главную", callback_data="start"),
    )
    return builder.as_markup()


def _topup_invoice_kb(method: str, order_id: str, pay_url: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💰 Перейти к оплате", url=pay_url))
    builder.row(InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"topup_check:{method}:{order_id}"))
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="topup_menu"),
        InlineKeyboardButton(text="🈴 На главную", callback_data="start"),
    )
    return builder.as_markup()


async def _show_topup_menu(target_message, telegram_id: int, force_new: bool = False):
    from database.requests import (
        get_user_internal_id, get_user_balance,
        is_cryptobot_configured, is_heleket_configured,
    )
    from bot.keyboards.admin import home_only_kb

    if not (is_cryptobot_configured() or is_heleket_configured()):
        await safe_edit_or_send(
            target_message,
            '💎 <b>Пополнение баланса</b>\n\n😔 Способы пополнения пока не настроены.',
            reply_markup=home_only_kb(), force_new=force_new,
        )
        return

    user_id = get_user_internal_id(telegram_id)
    balance = get_user_balance(user_id) if user_id else 0

    await safe_edit_or_send(
        target_message,
        f'💎 <b>Пополнение баланса</b>\n\n'
        f'Текущий баланс: <b>{_fmt_rub(balance)}</b>\n\n'
        f'Балансом можно оплачивать тарифы и автопродление ключей.\n\n'
        f'Выберите сумму пополнения:',
        reply_markup=_topup_amounts_kb(), force_new=force_new,
    )


@router.message(Command('topup'), StateFilter('*'))
async def cmd_topup(message: Message, state: FSMContext):
    """Команда /topup — меню пополнения баланса."""
    await state.clear()
    await _show_topup_menu(message, message.from_user.id, force_new=True)


@router.callback_query(F.data == 'topup_menu', StateFilter('*'))
async def topup_menu_handler(callback: CallbackQuery, state: FSMContext):
    """Меню пополнения баланса."""
    await _show_topup_menu(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith('topup_amt:'))
async def topup_amount_handler(callback: CallbackQuery):
    """Выбор способа оплаты после суммы."""
    from database.requests import is_cryptobot_configured, is_heleket_configured

    amount_rub = int(callback.data.split(':')[1])
    cryptobot = is_cryptobot_configured()
    heleket = is_heleket_configured()
    if not (cryptobot or heleket):
        await callback.answer('😔 Способы пополнения не настроены', show_alert=True)
        return

    await safe_edit_or_send(
        callback.message,
        f'💎 <b>Пополнение на {amount_rub} ₽</b>\n\nВыберите способ оплаты:',
        reply_markup=_topup_methods_kb(amount_rub, cryptobot, heleket),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('topup_pay:'))
async def topup_pay_handler(callback: CallbackQuery):
    """Создаёт инвойс на пополнение."""
    from database.requests import (
        get_user_internal_id, create_pending_topup_order, save_provider_invoice_id,
    )
    from bot.services.billing import create_cryptobot_invoice, create_heleket_payment
    from bot.keyboards.admin import home_only_kb
    from aiogram.types import BufferedInputFile

    parts = callback.data.split(':')
    method = parts[1]
    amount_rub = int(parts[2])
    if method not in ('cryptobot', 'heleket') or amount_rub < 10 or amount_rub > 100000:
        await callback.answer('❌ Некорректный запрос', show_alert=True)
        return

    user_id = get_user_internal_id(callback.from_user.id)
    if not user_id:
        await callback.answer('❌ Пользователь не найден', show_alert=True)
        return

    (_, order_id) = create_pending_topup_order(user_id, amount_rub * 100, method)
    await safe_edit_or_send(callback.message, '⏳ Создаём инвойс...')

    try:
        bot_info = await callback.bot.get_me()
        description = f"Пополнение баланса на {amount_rub} ₽"
        if method == 'cryptobot':
            result = await create_cryptobot_invoice(
                amount_rub=float(amount_rub), order_id=order_id,
                description=description, bot_name=bot_info.username,
            )
            title = '🤖 <b>Пополнение через CryptoBot</b>'
        else:
            result = await create_heleket_payment(
                amount_rub=float(amount_rub), order_id=order_id,
                description=description, bot_name=bot_info.username,
            )
            title = '🧿 <b>Пополнение через Heleket</b>'

        save_provider_invoice_id(order_id, result['provider_invoice_id'])
        pay_url = result['pay_url']
        text = (
            f"{title}\n\n"
            f"💰 <b>Сумма:</b> {amount_rub} ₽ (в крипте по курсу)\n\n"
            f"Оплатите по кнопке или QR-коду.\n\n"
            f"<i>После оплаты нажмите «✅ Я оплатил» — баланс зачислится автоматически.</i>"
        )
        photo = BufferedInputFile(result['qr_image_data'], filename='topup.png')
        await safe_edit_or_send(
            callback.message, text, photo=photo,
            reply_markup=_topup_invoice_kb(method, order_id, pay_url),
            force_new=True,
        )
    except (ValueError, RuntimeError) as e:
        logger.error(f'Ошибка создания инвойса пополнения ({method}): {e}')
        await safe_edit_or_send(
            callback.message,
            f'❌ <b>Ошибка создания платежа</b>\n\n<i>{escape_html(str(e))}</i>',
            reply_markup=home_only_kb(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith('topup_check:'))
async def topup_check_handler(callback: CallbackQuery):
    """Проверяет статус инвойса пополнения и зачисляет баланс."""
    from database.requests import (
        find_order_by_order_id, is_order_already_paid,
        get_user_internal_id, get_user_balance,
    )
    from bot.services.billing import (
        check_cryptobot_invoice_status, check_heleket_payment_status,
        process_payment_order,
    )
    from bot.keyboards.admin import home_only_kb

    parts = callback.data.split(':')
    method = parts[1]
    order_id = parts[2]

    def _balance_text(prefix: str) -> str:
        user_id = get_user_internal_id(callback.from_user.id)
        balance = get_user_balance(user_id) if user_id else 0
        return f"{prefix}\n\n💎 Текущий баланс: <b>{_fmt_rub(balance)}</b>"

    if is_order_already_paid(order_id):
        await safe_edit_or_send(
            callback.message, _balance_text('✅ Этот платёж уже был обработан.'),
            reply_markup=home_only_kb(), force_new=True,
        )
        await callback.answer()
        return

    order = find_order_by_order_id(order_id)
    if not order:
        await callback.answer('❌ Ордер не найден', show_alert=True)
        return

    invoice_id = order.get('provider_invoice_id')
    if not invoice_id:
        await callback.answer('⚠️ Нет данных о платеже. Попробуйте чуть позже.', show_alert=True)
        return

    await callback.answer('🔍 Проверяем платёж...')
    try:
        if method == 'cryptobot':
            status = await check_cryptobot_invoice_status(invoice_id)
        else:
            status = await check_heleket_payment_status(invoice_id)
    except Exception as e:
        logger.error(f'Ошибка проверки пополнения {order_id}: {e}')
        await safe_edit_or_send(
            callback.message,
            '❌ Не удалось проверить статус платежа. Попробуйте позже.',
            reply_markup=home_only_kb(), force_new=True,
        )
        return

    if status == 'succeeded':
        (ok, msg, _) = await process_payment_order(order_id)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await safe_edit_or_send(
            callback.message, _balance_text(msg) if ok else msg,
            reply_markup=home_only_kb(), force_new=True,
        )
        # Алерт админам о платеже
        if ok:
            try:
                from bot.services.alerts import send_payment_alert
                fresh = find_order_by_order_id(order_id)
                if fresh:
                    await send_payment_alert(callback.bot, fresh)
            except Exception:
                pass
    elif status == 'canceled':
        await safe_edit_or_send(
            callback.message,
            '❌ <b>Инвойс истёк</b>\n\nСоздайте пополнение заново.',
            reply_markup=home_only_kb(), force_new=True,
        )
    else:
        await safe_edit_or_send(
            callback.message,
            '⏳ <b>Платёж ещё не поступил</b>\n\nОплатите инвойс и нажмите «✅ Я оплатил» снова.',
            reply_markup=callback.message.reply_markup,
        )
