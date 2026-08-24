"""
Активация промокодов пользователем.

Пути активации:
- Кнопка «🎟️ Промокод» на главной (callback promo_enter) → FSM-ввод кода
- Команда /promo CODE
- Deep-link /start promo_{code} (обрабатывается в start.py)
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.states.user_states import PromoInput
from bot.utils.text import safe_edit_or_send
from database.requests import get_or_create_user, activate_promo

logger = logging.getLogger(__name__)

router = Router()


def _promo_result_kb():
    """Клавиатура после активации: на главную."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🈴 На главную", callback_data="start"))
    return builder.as_markup()


def _promo_cancel_kb():
    """Клавиатура при вводе кода: отмена."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="start"))
    return builder.as_markup()


async def _activate_and_reply(message: Message, user_db_id: int, code: str, edit: bool = False):
    """Активирует код и отправляет результат пользователю."""
    from bot.services.billing import apply_promo_effect

    (ok, msg, promo) = activate_promo(code, user_db_id)
    if ok and promo:
        msg = await apply_promo_effect(user_db_id, promo)

    await safe_edit_or_send(
        message, msg,
        reply_markup=_promo_result_kb(),
        force_new=not edit,
    )


@router.callback_query(F.data == "promo_enter", StateFilter('*'))
async def promo_enter_handler(callback: CallbackQuery, state: FSMContext):
    """Кнопка «Промокод»: просим ввести код."""
    (user, _) = get_or_create_user(callback.from_user.id, callback.from_user.username)
    if user.get('is_banned'):
        await callback.answer("⛔ Доступ заблокирован", show_alert=True)
        return

    await state.set_state(PromoInput.waiting_for_code)

    await safe_edit_or_send(
        callback.message,
        "🎟️ <b>Активация промокода</b>\n\n"
        "Отправьте промокод сообщением.\n\n"
        "<i>Промокод может дать: дни подписки, скидку на покупку, "
        "пополнение баланса или пробный доступ.</i>",
        reply_markup=_promo_cancel_kb(),
    )
    await callback.answer()


@router.message(PromoInput.waiting_for_code, F.text)
async def promo_code_input_handler(message: Message, state: FSMContext):
    """Обрабатывает введённый промокод."""
    await state.clear()

    (user, _) = get_or_create_user(message.from_user.id, message.from_user.username)
    if user.get('is_banned'):
        return

    code = (message.text or '').strip().split()[0] if message.text else ''
    if not code or len(code) > 64:
        await safe_edit_or_send(
            message, "❌ Некорректный промокод.",
            reply_markup=_promo_result_kb(), force_new=True,
        )
        return

    logger.info(f"User {message.from_user.id} активирует промокод '{code}'")
    await _activate_and_reply(message, user['id'], code)


@router.message(Command('promo'), StateFilter('*'))
async def cmd_promo(message: Message, state: FSMContext, command: CommandObject):
    """Команда /promo CODE — активация промокода."""
    await state.clear()

    (user, _) = get_or_create_user(message.from_user.id, message.from_user.username)
    if user.get('is_banned'):
        return

    code = (command.args or '').strip()
    if not code:
        await state.set_state(PromoInput.waiting_for_code)
        await safe_edit_or_send(
            message,
            "🎟️ <b>Активация промокода</b>\n\nОтправьте промокод сообщением.",
            reply_markup=_promo_cancel_kb(),
            force_new=True,
        )
        return

    logger.info(f"User {message.from_user.id} активирует промокод '{code}' через /promo")
    await _activate_and_reply(message, user['id'], code.split()[0])
