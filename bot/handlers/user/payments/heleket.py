"""
Оплата через Heleket (крипто-эквайринг, ex-Cryptomus).

Сумма фиксируется в рублях, пользователь платит любой поддерживаемой
криптовалютой на странице Heleket.
Поток идентичен WATA: pending order → платёж → «✅ Я оплатил» (или вебхук).
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.utils.text import escape_html, safe_edit_or_send
from bot.handlers.user.payments.base import finalize_payment_ui

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == 'pay_heleket')
async def pay_heleket_select_tariff(callback: CallbackQuery):
    """Выбор тарифа для оплаты через Heleket (новый ключ)."""
    from database.requests import get_all_tariffs
    from bot.keyboards.user import tariff_select_kb
    from bot.keyboards.admin import home_only_kb

    tariffs = get_all_tariffs(include_hidden=False)
    rub_tariffs = [t for t in tariffs if t.get('price_rub') and t['price_rub'] >= 1]
    if not rub_tariffs:
        await safe_edit_or_send(
            callback.message,
            '🧿 <b>Оплата Heleket</b>\n\n😔 Нет тарифов с ценой в рублях.\nОбратитесь к администратору.',
            reply_markup=home_only_kb()
        )
        await callback.answer()
        return
    await safe_edit_or_send(
        callback.message,
        '🧿 <b>Оплата Heleket (криптовалюта)</b>\n\nВыберите тариф:\n\n'
        '<i>Оплата USDT, TON, BTC, ETH и другими монетами.</i>',
        reply_markup=tariff_select_kb(rub_tariffs, is_heleket=True)
    )
    await callback.answer()


async def _create_heleket_payment_flow(
    callback: CallbackQuery,
    tariff: dict,
    order_id: str,
    back_callback: str,
    price_rub: float,
    extra_lines: str = ""
) -> None:
    """Создаёт платёж Heleket и отправляет пользователю сообщение с QR."""
    from database.requests import save_provider_invoice_id
    from bot.services.billing import create_heleket_payment
    from bot.keyboards.user import heleket_qr_kb
    from bot.keyboards.admin import home_only_kb

    await safe_edit_or_send(callback.message, '⏳ Создаём платёж...')
    try:
        bot_info = await callback.bot.get_me()
        result = await create_heleket_payment(
            amount_rub=price_rub, order_id=order_id,
            description=tariff['name'], bot_name=bot_info.username
        )
        save_provider_invoice_id(order_id, result['provider_invoice_id'])
        pay_url = result['pay_url']
        text = (
            f"🧿 <b>Оплата Heleket</b>\n\n"
            f"{extra_lines}"
            f"💳 <b>Тариф:</b> {escape_html(tariff['name'])}\n"
            f"💰 <b>Сумма:</b> {int(price_rub)} ₽ (в крипте по курсу)\n"
            f"⏳ <b>Срок:</b> {tariff['duration_days']} дней\n\n"
            f"Отсканируйте QR-код или перейдите по "
            f"<a href=\"{pay_url}\">ссылке на оплату</a>.\n\n"
            f"<i>После оплаты нажмите «✅ Я оплатил».</i>"
        )
        from aiogram.types import BufferedInputFile
        photo = BufferedInputFile(result['qr_image_data'], filename='heleket.png')
        await safe_edit_or_send(
            callback.message, text, photo=photo,
            reply_markup=heleket_qr_kb(order_id, back_callback=back_callback, qr_url=pay_url),
            force_new=True
        )
    except (ValueError, RuntimeError) as e:
        logger.error(f'Ошибка создания Heleket-платежа: {e}')
        await safe_edit_or_send(
            callback.message,
            f'❌ <b>Ошибка создания платежа</b>\n\n<i>{escape_html(str(e))}</i>\n\nПопробуйте другой способ оплаты.',
            reply_markup=home_only_kb()
        )


@router.callback_query(F.data.startswith('heleket_pay:'))
async def heleket_pay_create(callback: CallbackQuery):
    """Создаёт платёж Heleket для нового ключа."""
    from database.requests import get_tariff_by_id, get_user_internal_id, create_pending_order_from_tariff
    from bot.services.billing import get_discounted_tariff

    tariff_id = int(callback.data.split(':')[1])
    tariff = get_tariff_by_id(tariff_id)
    if not tariff:
        await callback.answer('❌ Тариф не найден', show_alert=True)
        return
    user_id = get_user_internal_id(callback.from_user.id)
    if not user_id:
        await callback.answer('❌ Пользователь не найден', show_alert=True)
        return
    tariff = get_discounted_tariff(user_id, tariff)
    price_rub = float(tariff.get('price_rub') or 0)
    if price_rub < 1:
        await callback.answer('❌ У тарифа нет цены в рублях', show_alert=True)
        return
    (_, order_id) = create_pending_order_from_tariff(
        user_id=user_id, tariff=tariff, payment_type='heleket', vpn_key_id=None
    )
    await _create_heleket_payment_flow(callback, tariff, order_id, 'pay_heleket', price_rub)
    await callback.answer()


@router.callback_query(F.data.startswith('renew_heleket_tariff:'))
async def renew_heleket_select_tariff(callback: CallbackQuery):
    """Выбор тарифа для оплаты Heleket при продлении ключа."""
    from database.requests import get_key_details_for_user
    from bot.keyboards.user import renew_tariff_select_kb
    from bot.utils.groups import get_tariffs_for_renewal

    key_id = int(callback.data.split(':')[1])
    key = get_key_details_for_user(key_id, callback.from_user.id)
    if not key:
        await callback.answer('❌ Ключ не найден', show_alert=True)
        return
    tariffs = get_tariffs_for_renewal(key.get('tariff_id', 0))
    rub_tariffs = [t for t in tariffs if t.get('price_rub') and t['price_rub'] >= 1]
    if not rub_tariffs:
        await callback.answer('😔 Нет тарифов с ценой в рублях', show_alert=True)
        return
    await safe_edit_or_send(
        callback.message,
        f"🧿 <b>Оплата Heleket</b>\n\n🔑 Ключ: <b>{escape_html(key['display_name'])}</b>\n\nВыберите тариф для продления:",
        reply_markup=renew_tariff_select_kb(rub_tariffs, key_id, is_heleket=True)
    )
    await callback.answer()


@router.callback_query(F.data.startswith('renew_pay_heleket:'))
async def renew_heleket_create(callback: CallbackQuery):
    """Создаёт платёж Heleket для продления ключа."""
    from database.requests import (
        get_tariff_by_id, get_user_internal_id, create_pending_order_from_tariff, get_key_details_for_user
    )
    from bot.services.billing import get_discounted_tariff

    parts = callback.data.split(':')
    key_id = int(parts[1])
    tariff_id = int(parts[2])
    tariff = get_tariff_by_id(tariff_id)
    key = get_key_details_for_user(key_id, callback.from_user.id)
    if not tariff or not key:
        await callback.answer('❌ Ошибка тарифа или ключа', show_alert=True)
        return
    user_id = get_user_internal_id(callback.from_user.id)
    if not user_id:
        await callback.answer('❌ Пользователь не найден', show_alert=True)
        return
    tariff = get_discounted_tariff(user_id, tariff)
    price_rub = float(tariff.get('price_rub') or 0)
    if price_rub < 1:
        await callback.answer('❌ У тарифа нет цены в рублях', show_alert=True)
        return
    (_, order_id) = create_pending_order_from_tariff(
        user_id=user_id, tariff=tariff, payment_type='heleket', vpn_key_id=key_id
    )
    extra = f"🔑 <b>Ключ:</b> {escape_html(key['display_name'])}\n"
    await _create_heleket_payment_flow(
        callback, tariff, order_id, f'renew_heleket_tariff:{key_id}', price_rub, extra_lines=extra
    )
    await callback.answer()


@router.callback_query(F.data.startswith('check_heleket:'))
async def check_heleket_payment(callback: CallbackQuery, state: FSMContext):
    """Проверяет статус платежа Heleket по нажатию «✅ Я оплатил»."""
    from database.requests import find_order_by_order_id, is_order_already_paid
    from bot.services.billing import check_heleket_payment_status, complete_payment_flow
    from bot.keyboards.admin import home_only_kb

    order_id = callback.data.split(':', 1)[1]

    if is_order_already_paid(order_id):
        order = find_order_by_order_id(order_id)
        if order:
            await finalize_payment_ui(
                callback.message, state,
                '✅ Оплата уже была обработана ранее.',
                order, user_id=callback.from_user.id
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
        status = await check_heleket_payment_status(invoice_id)
    except Exception as e:
        logger.error(f'Ошибка проверки статуса Heleket {order_id}: {e}')
        await safe_edit_or_send(
            callback.message,
            '❌ Не удалось проверить статус платежа. Попробуйте позже.',
            reply_markup=home_only_kb(), force_new=True
        )
        return

    if status == 'succeeded':
        try:
            await callback.message.delete()
        except Exception:
            pass
        await complete_payment_flow(
            order_id=order_id,
            message=callback.message,
            state=state,
            telegram_id=callback.from_user.id,
            payment_type='heleket',
            referral_amount=0
        )
    elif status == 'canceled':
        await safe_edit_or_send(
            callback.message,
            '❌ <b>Платёж отменён или истёк</b>\n\nСоздайте платёж заново, выбрав тариф.',
            reply_markup=home_only_kb(), force_new=True
        )
    else:
        await safe_edit_or_send(
            callback.message,
            '⏳ <b>Платёж ещё не поступил</b>\n\nОплатите по ссылке и нажмите «✅ Я оплатил» снова.\n\n'
            '<i>Криптоплатёж может подтверждаться несколько минут.</i>',
            reply_markup=callback.message.reply_markup,
        )
