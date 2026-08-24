import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()


async def _check_trial_channel_gate(callback: CallbackQuery) -> bool:
    """
    Гейт пробной подписки: обязательная подписка на канал (анти-фрод).

    Returns:
        True — доступ разрешён (гейт выключен или юзер подписан)
    """
    from database.requests import get_trial_channel

    (channel_id, channel_link) = get_trial_channel()
    if not channel_id:
        return True

    try:
        member = await callback.bot.get_chat_member(chat_id=channel_id, user_id=callback.from_user.id)
        if member.status in ('member', 'administrator', 'creator'):
            return True
    except Exception as e:
        # Канал недоступен боту (не админ / неверный ID) — не блокируем юзеров
        logger.warning(f'Trial-гейт: не удалось проверить подписку на {channel_id}: {e}')
        return True

    builder = InlineKeyboardBuilder()
    if channel_link:
        builder.row(InlineKeyboardButton(text='📢 Подписаться на канал', url=channel_link))
    builder.row(InlineKeyboardButton(text='✅ Я подписался', callback_data='trial_activate'))
    builder.row(InlineKeyboardButton(text='🈴 На главную', callback_data='start'))

    await safe_edit_or_send(
        callback.message,
        '🎁 <b>Пробная подписка</b>\n\n'
        'Чтобы активировать пробный период, подпишитесь на наш канал '
        'и нажмите «✅ Я подписался».',
        reply_markup=builder.as_markup(),
    )
    await callback.answer()
    return False


@router.callback_query(F.data == 'trial_subscription')
async def show_trial_subscription(callback: CallbackQuery):
    """Показывает страницу пробной подписки."""
    from database.requests import is_trial_enabled, get_trial_tariff_id, has_used_trial
    from bot.utils.page_renderer import render_page

    user_id = callback.from_user.id

    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return
    if get_trial_tariff_id() is None:
        await callback.answer('❌ Тариф не настроен', show_alert=True)
        return
    if has_used_trial(user_id):
        await callback.answer('ℹ️ Вы уже использовали пробный период', show_alert=True)
        return

    await render_page(callback, page_key='trial')
    await callback.answer()


@router.callback_query(F.data == 'trial_activate')
async def activate_trial_subscription(callback: CallbackQuery, state: FSMContext):
    """Активирует пробную подписку: создаёт ключ через стандартный механизм."""
    from database.requests import is_trial_enabled, get_trial_tariff_id, has_used_trial, get_tariff_by_id, get_or_create_user, mark_trial_used, create_initial_vpn_key, create_pending_order, complete_order
    from bot.handlers.user.payments.keys_config import start_new_key_config

    user_id = callback.from_user.id

    if not is_trial_enabled():
        await callback.answer('❌ Пробная подписка недоступна', show_alert=True)
        return
    tariff_id = get_trial_tariff_id()
    if tariff_id is None:
        await callback.answer('❌ Тариф не настроен', show_alert=True)
        return
    if has_used_trial(user_id):
        await callback.answer('ℹ️ Вы уже использовали пробный период', show_alert=True)
        return

    if not await _check_trial_channel_gate(callback):
        return

    tariff = get_tariff_by_id(tariff_id)
    if not tariff:
        await callback.answer('❌ Тариф не найден', show_alert=True)
        return

    (user, _) = get_or_create_user(user_id, callback.from_user.username)
    internal_user_id = user['id']
    duration_days = tariff['duration_days']
    traffic_limit_bytes = (tariff.get('traffic_limit_gb', 0) or 0) * 1024 ** 3

    try:
        key_id = create_initial_vpn_key(
            internal_user_id, tariff_id, duration_days, traffic_limit=traffic_limit_bytes,
        )
        (_, order_id) = create_pending_order(
            user_id=internal_user_id, tariff_id=tariff_id,
            payment_type='trial', vpn_key_id=key_id,
        )
        complete_order(order_id)
        mark_trial_used(internal_user_id)
        logger.info(f'Пользователь {user_id} активировал пробный период (тариф ID={tariff_id})')
    except Exception:
        logger.exception('Ошибка активации триала для user %s', user_id)
        await callback.answer('❌ Не удалось создать пробный ключ. Попробуйте позже.', show_alert=True)
        return

    await state.update_data(new_key_order_id=order_id, new_key_id=key_id)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await start_new_key_config(callback.message, state, order_id, key_id)