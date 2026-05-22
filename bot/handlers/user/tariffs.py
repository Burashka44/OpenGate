import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == 'buy_key')
async def buy_key_handler(callback: CallbackQuery):
    """Страница «Купить ключ» с условиями и способами оплаты."""
    from database.requests import (
        is_crypto_configured, is_stars_enabled, is_cards_enabled,
        is_yookassa_qr_configured, is_wata_configured, is_platega_configured,
        is_cardlink_configured,
        is_demo_payment_enabled,
        get_user_internal_id, create_pending_order,
        get_setting,
    )
    from bot.utils.page_renderer import render_page
    from bot.keyboards.admin import home_only_kb

    telegram_id = callback.from_user.id
    crypto_configured = is_crypto_configured()
    stars_enabled = is_stars_enabled()
    cards_enabled = is_cards_enabled()
    yookassa_qr = is_yookassa_qr_configured()
    wata_enabled = is_wata_configured()
    platega_enabled = is_platega_configured()
    cardlink_enabled = is_cardlink_configured()
    demo_enabled = is_demo_payment_enabled()

    # Проверка: хотя бы один способ оплаты настроен
    if not crypto_configured and not stars_enabled and not cards_enabled and not yookassa_qr and not wata_enabled and not platega_enabled and not cardlink_enabled and not demo_enabled:
        await safe_edit_or_send(
            callback.message,
            '💳 <b>Купить ключ</b>\n\n😔 К сожалению, сейчас оплата недоступна.\n\nПопробуйте позже или обратитесь в поддержку.',
            reply_markup=home_only_kb()
        )
        await callback.answer()
        return

    # Создаём pending order для контекста system-кнопок
    user_id = get_user_internal_id(telegram_id)
    order_id = None
    if user_id:
        (_, order_id) = create_pending_order(user_id=user_id, tariff_id=None, payment_type=None, vpn_key_id=None)

    # Контекст для system-кнопок оплаты
    context = {
        'order_id': order_id,
        'telegram_id': telegram_id,
    }

    await render_page(
        callback,
        page_key='prepayment',
        context=context,
    )
    await callback.answer()