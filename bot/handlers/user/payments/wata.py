import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.utils.text import escape_html, safe_edit_or_send
from bot.handlers.user.payments.base import finalize_payment_ui

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == 'pay_wata')
async def pay_wata_select_tariff(callback: CallbackQuery):
    """Выбор тарифа для оплаты через WATA (новый ключ)."""
    from database.requests import get_all_tariffs
    from bot.keyboards.user import tariff_select_kb
    from bot.keyboards.admin import home_only_kb

    tariffs = get_all_tariffs(include_hidden=False)
    # WATA: минимум 10 ₽
    rub_tariffs = [t for t in tariffs if t.get('price_rub') and t['price_rub'] >= 10]
    if not rub_tariffs:
        await safe_edit_or_send(
            callback.message,
            '🌊 <b>Оплата WATA</b>\n\n😔 Нет тарифов с ценой в рублях (от 10 ₽).\nОбратитесь к администратору.',
            reply_markup=home_only_kb()
        )
        await callback.answer()
        return
    await safe_edit_or_send(
        callback.message,
        '🌊 <b>Оплата WATA (Карта/СБП)</b>\n\nВыберите тариф:\n\n<i>Оплата через WATA — поддерживает банковские карты и СБП.</i>',
        reply_markup=tariff_select_kb(rub_tariffs, is_wata=True)
    )
    await callback.answer()


@router.callback_query(F.data.startswith('wata_pay:'))
async def wata_pay_create(callback: CallbackQuery):
    """Создаёт платёжную ссылку WATA для нового ключа и отправляет QR-фото."""
    from database.requests import (
        get_tariff_by_id, get_user_internal_id, create_pending_order, save_wata_link_id
    )
    from bot.services.billing import create_wata_payment
    from bot.keyboards.user import wata_qr_kb
    from bot.keyboards.admin import home_only_kb

    tariff_id = int(callback.data.split(':')[1])
    tariff = get_tariff_by_id(tariff_id)
    if not tariff:
        await callback.answer('❌ Тариф не найден', show_alert=True)
        return
    price_rub = float(tariff.get('price_rub') or 0)
    if price_rub < 10:
        await callback.answer('❌ Минимальная сумма для WATA — 10 ₽', show_alert=True)
        return
    user_id = get_user_internal_id(callback.from_user.id)
    if not user_id:
        await callback.answer('❌ Пользователь не найден', show_alert=True)
        return
    (_, order_id) = create_pending_order(
        user_id=user_id, tariff_id=tariff_id, payment_type='wata', vpn_key_id=None
    )
    await safe_edit_or_send(callback.message, '⏳ Создаём ссылку на оплату...')
    try:
        bot_info = await callback.bot.get_me()
        bot_name = bot_info.username
        description = f"Покупка «{tariff['name']}» — {tariff['duration_days']} дней"
        result = await create_wata_payment(
            amount_rub=price_rub, order_id=order_id, description=description, bot_name=bot_name
        )
        save_wata_link_id(order_id, result['wata_link_id'])
        qr_image_data = result.get('qr_image_data')
        qr_url = result.get('qr_url', '')
        if not qr_image_data or not qr_url:
            await safe_edit_or_send(
                callback.message,
                '❌ WATA не вернула данные для оплаты. Попробуйте позже.',
                reply_markup=home_only_kb()
            )
            return
        text = (
            f"🌊 <b>Оплата WATA</b>\n\n"
            f"💳 <b>Тариф:</b> {escape_html(tariff['name'])}\n"
            f"💰 <b>Сумма:</b> {int(price_rub)} ₽\n"
            f"⏳ <b>Срок:</b> {tariff['duration_days']} дней\n\n"
            f"Отсканируйте QR-код банковским приложением или перейдите по "
            f"<a href=\"{qr_url}\">ссылке на оплату</a>.\n\n"
            f"<i>После оплаты нажмите «✅ Я оплатил».</i>"
        )
        from aiogram.types import BufferedInputFile
        photo = BufferedInputFile(qr_image_data, filename='wata.png')
        await safe_edit_or_send(
            callback.message,
            text,
            photo=photo,
            reply_markup=wata_qr_kb(order_id, back_callback='pay_wata', qr_url=qr_url),
            force_new=True
        )
    except (ValueError, RuntimeError) as e:
        logger.error(f'Ошибка создания WATA-ссылки: {e}')
        await safe_edit_or_send(
            callback.message,
            f'❌ <b>Ошибка создания платежа</b>\n\n<i>{escape_html(str(e))}</i>\n\nПопробуйте другой способ оплаты.',
            reply_markup=home_only_kb()
        )
    await callback.answer()


@router.callback_query(F.data.startswith('renew_wata_tariff:'))
async def renew_wata_select_tariff(callback: CallbackQuery):
    """Выбор тарифа для оплаты WATA при продлении ключа."""
    from database.requests import get_key_details_for_user
    from bot.keyboards.user import renew_tariff_select_kb
    from bot.utils.groups import get_tariffs_for_renewal

    key_id = int(callback.data.split(':')[1])
    key = get_key_details_for_user(key_id, callback.from_user.id)
    if not key:
        await callback.answer('❌ Ключ не найден', show_alert=True)
        return
    tariffs = get_tariffs_for_renewal(key.get('tariff_id', 0))
    rub_tariffs = [t for t in tariffs if t.get('price_rub') and t['price_rub'] >= 10]
    if not rub_tariffs:
        await callback.answer('😔 Нет тарифов с ценой в рублях (от 10 ₽)', show_alert=True)
        return
    await safe_edit_or_send(
        callback.message,
        f"🌊 <b>Оплата WATA (Карта/СБП)</b>\n\n🔑 Ключ: <b>{escape_html(key['display_name'])}</b>\n\nВыберите тариф для продления:",
        reply_markup=renew_tariff_select_kb(rub_tariffs, key_id, is_wata=True)
    )
    await callback.answer()


@router.callback_query(F.data.startswith('renew_pay_wata:'))
async def renew_wata_create(callback: CallbackQuery):
    """Создаёт платёжную ссылку WATA для продления ключа."""
    from database.requests import (
        get_tariff_by_id, get_user_internal_id, create_pending_order,
        save_wata_link_id, get_key_details_for_user
    )
    from bot.services.billing import create_wata_payment
    from bot.keyboards.user import wata_qr_kb
    from bot.keyboards.admin import home_only_kb

    parts = callback.data.split(':')
    key_id = int(parts[1])
    tariff_id = int(parts[2])
    tariff = get_tariff_by_id(tariff_id)
    key = get_key_details_for_user(key_id, callback.from_user.id)
    if not tariff or not key:
        await callback.answer('❌ Ошибка тарифа или ключа', show_alert=True)
        return
    price_rub = float(tariff.get('price_rub') or 0)
    if price_rub < 10:
        await callback.answer('❌ Минимальная сумма для WATA — 10 ₽', show_alert=True)
        return
    user_id = get_user_internal_id(callback.from_user.id)
    if not user_id:
        await callback.answer('❌ Пользователь не найден', show_alert=True)
        return
    (_, order_id) = create_pending_order(
        user_id=user_id, tariff_id=tariff_id, payment_type='wata', vpn_key_id=key_id
    )
    await safe_edit_or_send(callback.message, '⏳ Создаём ссылку на оплату...')
    try:
        bot_info = await callback.bot.get_me()
        bot_name = bot_info.username
        description = f"Продление Ключа «{key['display_name']}»: «{tariff['name']}» ({tariff['duration_days']} дн.)"
        result = await create_wata_payment(
            amount_rub=price_rub, order_id=order_id, description=description, bot_name=bot_name
        )
        save_wata_link_id(order_id, result['wata_link_id'])
        qr_image_data = result.get('qr_image_data')
        qr_url = result.get('qr_url', '')
        if not qr_image_data or not qr_url:
            await safe_edit_or_send(
                callback.message,
                '❌ WATA не вернула данные для оплаты. Попробуйте позже.',
                reply_markup=home_only_kb()
            )
            return
        text = (
            f"🌊 <b>Оплата WATA</b>\n\n"
            f"🔑 <b>Ключ:</b> {escape_html(key['display_name'])}\n"
            f"💳 <b>Тариф:</b> {escape_html(tariff['name'])}\n"
            f"💰 <b>Сумма:</b> {int(price_rub)} ₽\n"
            f"⏳ <b>Продление:</b> +{tariff['duration_days']} дней\n\n"
            f"Отсканируйте QR-код банковским приложением или перейдите по "
            f"<a href=\"{qr_url}\">ссылке на оплату</a>.\n\n"
            f"<i>После оплаты нажмите «✅ Я оплатил».</i>"
        )
        from aiogram.types import BufferedInputFile
        photo = BufferedInputFile(qr_image_data, filename='wata.png')
        await safe_edit_or_send(
            callback.message,
            text,
            photo=photo,
            reply_markup=wata_qr_kb(order_id, back_callback=f'renew_wata_tariff:{key_id}', qr_url=qr_url),
            force_new=True
        )
    except (ValueError, RuntimeError) as e:
        logger.error(f'Ошибка WATA (продление): {e}')
        await safe_edit_or_send(
            callback.message,
            f'❌ <b>Ошибка создания платежа</b>\n\n<i>{escape_html(str(e))}</i>',
            reply_markup=home_only_kb()
        )
    await callback.answer()


@router.callback_query(F.data.startswith('check_wata:'))
async def check_wata_payment(callback: CallbackQuery, state: FSMContext):
    """
    Проверяет статус WATA-платежа по нажатию «✅ Я оплатил».

    WATA имеет лимит — не чаще одного запроса в 30 секунд.
    На стороне пользователя контролируем это, кэшируя время последней проверки в FSM.
    При успехе — делегирует обработку в complete_payment_flow().
    """
    import time
    from database.requests import find_order_by_order_id, is_order_already_paid, update_payment_type
    from bot.services.billing import check_wata_payment_status, complete_payment_flow
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

    wata_link_id = order.get('wata_link_id')
    if not wata_link_id:
        await callback.answer('⚠️ Нет данных о платеже. Попробуйте чуть позже.', show_alert=True)
        return

    # Защита от частых запросов (WATA: 1 запрос в 30 сек по одному order_id)
    state_data = await state.get_data()
    last_check_key = f'wata_last_check_{order_id}'
    last_check = state_data.get(last_check_key, 0)
    now = time.time()
    elapsed = now - last_check
    if last_check and elapsed < 30:
        wait = int(30 - elapsed)
        await callback.answer(
            f'⏳ Подождите {wait} сек. перед повторной проверкой.',
            show_alert=True
        )
        return
    await state.update_data({last_check_key: now})

    await callback.answer('🔍 Проверяем платёж...')
    try:
        status = await check_wata_payment_status(order_id)
    except Exception as e:
        logger.error(f'Ошибка проверки статуса WATA {order_id}: {e}')
        await safe_edit_or_send(
            callback.message,
            '❌ Не удалось проверить статус платежа. Попробуйте позже.',
            reply_markup=home_only_kb(), force_new=True
        )
        return

    if status == 'succeeded':
        update_payment_type(order_id, 'wata')
        from database.requests import get_tariff_by_id
        _tariff = get_tariff_by_id(order.get('tariff_id'))
        referral_amount = int((_tariff.get('price_rub', 0) or 0) * 100) if _tariff else 0
        logger.info(f"WATA referral: order={order_id}, referral_amount={referral_amount}")
        try:
            await callback.message.delete()
        except Exception:
            pass
        await complete_payment_flow(
            order_id=order_id,
            message=callback.message,
            state=state,
            telegram_id=callback.from_user.id,
            payment_type='wata',
            referral_amount=referral_amount
        )
    elif status == 'canceled':
        await safe_edit_or_send(
            callback.message,
            '❌ <b>Платёж отменён</b>\n\nПохоже, платёж был отменён.\nПопробуйте снова выбрать тариф.',
            reply_markup=home_only_kb(), force_new=True
        )
    else:
        await safe_edit_or_send(
            callback.message,
            '⏳ <b>Платёж ещё не поступил</b>\n\nОплатите по ссылке и нажмите «✅ Я оплатил» снова.\n\n<i>Если только что оплатили — подождите 30 секунд (ограничение WATA API).</i>',
            force_new=True
        )
