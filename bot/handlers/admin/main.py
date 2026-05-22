"""
Главный роутер админ-панели.

Обрабатывает вход в админку и главное меню.
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import ADMIN_IDS
from database.requests import get_all_servers
from bot.services.vpn_api import get_client_from_server_data, format_traffic
from bot.states.admin_states import AdminStates
from bot.keyboards.admin import admin_main_menu_kb, home_only_kb, project_support_kb
from database.requests import get_setting
from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router()


# ============================================================================
# ПРОВЕРКА АДМИНИСТРАТОРА
# ============================================================================




# ============================================================================
# ГЛАВНОЕ МЕНЮ АДМИНКИ
# ============================================================================

async def get_admin_stats_text() -> str:
    """
    Формирует текст со статистикой всех серверов.
    
    Returns:
        Отформатированный текст для сообщения
    """
    servers = get_all_servers()
    
    if not servers:
        return (
            "⚙️ <b>Админ-панель</b>\n\n"
            "🖥️ Серверов пока нет.\n"
            "Добавьте первый сервер в разделе «Сервера»."
        )
    
    lines = ["⚙️ <b>Админ-панель</b>\n"]
    
    for server in servers:
        status_emoji = "🟢" if server['is_active'] else "🔴"
        lines.append(f"{status_emoji} <b>{server['name']}</b> (<code>{server['host']}:{server['port']}</code>)")
        
        if server['is_active']:
            # Пробуем получить статистику
            try:
                client = get_client_from_server_data(server)
                stats = await client.get_stats()
                
                if stats.get('online'):
                    traffic = format_traffic(stats.get('total_traffic_bytes', 0))
                    active = stats.get('active_clients', 0)
                    online = stats.get('online_clients', 0)
                    
                    cpu_text = ""
                    if stats.get('cpu_percent') is not None:
                        cpu_text = f" | 💻 {stats['cpu_percent']}% CPU"
                    
                    lines.append(f"   🔑 {online} онлайн | 📊 {traffic}{cpu_text}")
                else:
                    error = stats.get('error', 'Нет подключения')
                    lines.append(f"   ⚠️ {error}")
            except Exception as e:
                logger.warning(f"Ошибка получения статистики {server['name']}: {e}")
                lines.append(f"   ⚠️ Ошибка подключения")
        else:
            lines.append("   ⏸️ Деактивирован")
        
        lines.append("")  # Пустая строка между серверами
    
    return "\n".join(lines)


from aiogram.exceptions import TelegramBadRequest

@router.callback_query(F.data == "admin_panel")
async def show_admin_panel(callback: CallbackQuery, state: FSMContext):
    """Показывает главное меню админ-панели."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    await state.set_state(AdminStates.admin_menu)
    from bot.services.page_context import clear_page_context
    clear_page_context(callback.from_user.id)

    # Снимаем застрявшую Reply-клавиатуру (например, после поиска пользователя)
    import asyncio
    from aiogram.types import ReplyKeyboardRemove
    try:
        temp_msg = await callback.message.answer("⏳", reply_markup=ReplyKeyboardRemove())
        async def _delete_temp():
            await asyncio.sleep(2.0)
            try:
                await temp_msg.delete()
            except Exception:
                pass
        asyncio.create_task(_delete_temp())
    except Exception:
        pass

    text = await get_admin_stats_text()
    
    try:
        await safe_edit_or_send(callback.message, 
            text,
            reply_markup=admin_main_menu_kb()
        )
    except TelegramBadRequest as e:
        if "is not modified" not in str(e):
            logger.error(f"Ошибка при обновлении меню: {e}")


# ============================================================================
# РАЗДЕЛ ПОДДЕРЖКИ
# ============================================================================

@router.callback_query(F.data == "admin_project_support")
async def show_project_support(callback: CallbackQuery):
    """Показывает экран поддержки проекта (ссылки из settings)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await callback.answer()

    title = get_setting('project_support_title', '') or 'Поддержка проекта'
    body = (get_setting('project_support_text', '') or '').strip()
    card_url = (get_setting('project_support_donation_card_url', '') or '').strip()
    crypto_url = (get_setting('project_support_donation_crypto_url', '') or '').strip()
    extra_url = (get_setting('project_support_extra_url', '') or '').strip()
    extra_label = (get_setting('project_support_extra_label', '') or '').strip()

    if body:
        text = f"💝 <b>{title}</b>\n\n{body}"
    else:
        text = f"💝 <b>{title}</b>\n\n"

    if not body and not card_url and not crypto_url and not extra_url:
        text += (
            "Ссылки доната не настроены.\n\n"
            "Заполните ключи <code>project_support_*</code> в таблице settings "
            "или см. INTEGRATIONS.md."
        )
    
    try:
        await safe_edit_or_send(
            callback.message, 
            text,
            reply_markup=project_support_kb(
                card_url=card_url,
                crypto_url=crypto_url,
                extra_url=extra_url,
                extra_label=extra_label,
            )
        )
    except TelegramBadRequest as e:
        if "is not modified" not in str(e):
            logger.error(f"Ошибка при показе поддержки автора: {e}")

# ============================================================================
# ПЕРЕАДРЕСАЦИЯ НА ПОДРОУТЕРЫ
# ============================================================================

# Раздел «Пользователи» реализован в users.py
# Раздел «Настройки бота» реализован в system.py

