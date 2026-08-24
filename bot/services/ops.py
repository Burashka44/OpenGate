"""
Операционные фоновые задачи:

- healthcheck_loop — проверка доступности панелей каждые 5 минут;
  при падении всех серверов может автоматически включать maintenance_mode
  (healthcheck_auto_maintenance=1) и алертить админов.
- auto_renew_loop — автопродление ключей с баланса (раз в час).
"""
import asyncio
import logging
from typing import Dict

from aiogram import Bot

from database.requests import (
    get_setting, set_setting, get_active_servers, is_maintenance_mode, set_maintenance_mode,
    get_auto_renewable_keys, get_user_balance, deduct_from_balance,
    extend_vpn_key, is_auto_renew_enabled, is_notification_sent_today, log_notification_sent,
)

logger = logging.getLogger(__name__)

# Состояние последней проверки: server_id -> online
_last_health: Dict[int, bool] = {}


def _key_has_panel_fields(key: dict) -> bool:
    """Ключ готов к push: есть сервер и идентификатор клиента на панели."""
    if not key.get('server_id'):
        return False
    if key.get('client_uuid') or key.get('panel_email'):
        return True
    return False


async def check_servers_health(bot: Bot) -> None:
    """
    Один проход healthcheck: логин на каждую активную панель.

    - Алертит при смене состояния сервера (up→down, down→up)
    - Если упали ВСЕ панели — включает maintenance_mode (при auto=1) с флагом auto
    - Если хотя бы одна поднялась — выключает только auto-включённый maintenance
    """
    from bot.services.vpn_api import get_client_from_server_data
    from bot.services.alerts import send_admin_alert

    servers = get_active_servers()
    if not servers:
        return

    online_count = 0
    for server in servers:
        server_id = server['id']
        try:
            client = get_client_from_server_data(server)
            stats = await client.get_stats()
            is_online = bool(stats.get('online'))
        except Exception:
            is_online = False

        if is_online:
            online_count += 1
            from bot.services.circuit_breaker import record_success
            record_success(server_id)
        else:
            from bot.services.circuit_breaker import record_failure
            record_failure(server_id)

        prev = _last_health.get(server_id)
        if prev is not None and prev != is_online:
            emoji = "🟢" if is_online else "🔴"
            state = "снова доступен" if is_online else "НЕДОСТУПЕН"
            await send_admin_alert(
                bot, f"{emoji} Сервер <b>{server['name']}</b> {state}"
            )
        _last_health[server_id] = is_online

    auto = get_setting('healthcheck_auto_maintenance', '1') == '1'
    if not auto:
        return

    if online_count == 0 and not is_maintenance_mode():
        set_maintenance_mode(True)
        set_setting('maintenance_auto_set', 'auto')
        await send_admin_alert(
            bot,
            "🚧 Все серверы недоступны — <b>режим обслуживания включён автоматически</b>. "
            "Продажи приостановлены."
        )
    elif online_count > 0 and is_maintenance_mode() \
            and get_setting('maintenance_auto_set', '0') == 'auto':
        set_maintenance_mode(False)
        set_setting('maintenance_auto_set', '0')
        await send_admin_alert(
            bot,
            "✅ Серверы снова доступны — режим обслуживания выключен."
        )


async def healthcheck_loop(bot: Bot) -> None:
    """Фоновая задача healthcheck (каждые 5 минут, если включён)."""
    logger.info("❤️ Healthcheck-планировщик запущен")
    await asyncio.sleep(60)
    while True:
        try:
            if get_setting('healthcheck_enabled', '0') == '1':
                await check_servers_health(bot)
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            logger.info("Healthcheck-планировщик остановлен")
            break
        except Exception as e:
            logger.error(f"Ошибка healthcheck: {e}")
            await asyncio.sleep(300)


async def process_auto_renewals(bot: Bot) -> None:
    """
    Один проход автопродления: ключи с auto_renew=1, истекающие в течение суток.

    Порядок: push OK → deduct + extend уже в БД; при ошибке push — откат expires, баланс не трогаем.
    Insufficient funds: дедуп через notification_log (раз в сутки).
    """
    from bot.services.user_locks import user_locks
    from bot.services.vpn_api import (
        push_key_to_panel, restore_traffic_limit_in_db,
        ensure_subscription_keys_on_server,
    )
    from database.requests import get_vpn_key_by_id
    from database.connection import get_db

    keys = get_auto_renewable_keys(days_before=1)
    for key in keys:
        user_id = key['user_id']
        days = key.get('duration_days') or 30
        price_cents = int(round(float(key.get('price_rub') or 0) * 100))
        if price_cents <= 0:
            price_cents = int(key.get('price_cents') or 0)
        if price_cents <= 0:
            continue

        if not _key_has_panel_fields(key):
            logger.warning(f"auto-renew: ключ {key['id']} без panel-полей — пропуск")
            continue

        async with user_locks[user_id]:
            balance = get_user_balance(user_id)
            if balance < price_cents:
                if not is_notification_sent_today(key['id']):
                    try:
                        await bot.send_message(
                            chat_id=key['telegram_id'],
                            text=(
                                f"⚠️ <b>Автопродление не выполнено</b>\n\n"
                                f"Ключ <b>{key.get('custom_name') or '#' + str(key['id'])}</b> "
                                f"истекает, но на балансе недостаточно средств "
                                f"({balance / 100:g} ₽ из {price_cents / 100:g} ₽).\n\n"
                                f"Пополните баланс или продлите вручную."
                            ),
                            parse_mode='HTML'
                        )
                        log_notification_sent(key['id'])
                    except Exception:
                        pass
                continue

            # Snapshot expires для отката
            prev_expires = key.get('expires_at')
            extend_vpn_key(key['id'], days)
            restore_traffic_limit_in_db(key['id'])
            try:
                await push_key_to_panel(key['id'], reset_traffic=True)
            except Exception as e:
                logger.error(f"Автопродление ключа {key['id']}: push failed: {e}")
                if prev_expires:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE vpn_keys SET expires_at = ? WHERE id = ?",
                            (prev_expires, key['id'])
                        )
                continue

            if not deduct_from_balance(user_id, price_cents):
                logger.error(f"Автопродление ключа {key['id']}: не удалось списать баланс после push")
                if prev_expires:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE vpn_keys SET expires_at = ? WHERE id = ?",
                            (prev_expires, key['id'])
                        )
                try:
                    await push_key_to_panel(key['id'], reset_traffic=False)
                except Exception:
                    pass
                continue

        try:
            fresh = get_vpn_key_by_id(key['id'])
            if fresh and fresh.get('sub_id'):
                try:
                    await ensure_subscription_keys_on_server(key['id'])
                except Exception as e:
                    logger.warning(f"auto-renew: ensure_subscription({key['id']}) не удался: {e}")

            logger.info(
                f"Автопродление: ключ {key['id']} продлён на {days} дн., "
                f"списано {price_cents} коп"
            )
            try:
                await bot.send_message(
                    chat_id=key['telegram_id'],
                    text=(
                        f"🔄 <b>Автопродление выполнено</b>\n\n"
                        f"Ключ продлён на {days} дн.\n"
                        f"С баланса списано {price_cents / 100:g} ₽."
                    ),
                    parse_mode='HTML'
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Автопродление ключа {key['id']} post-notify: {e}")


async def auto_renew_loop(bot: Bot) -> None:
    """Фоновая задача автопродления (раз в час, если включено)."""
    logger.info("🔄 Планировщик автопродления запущен")
    await asyncio.sleep(120)
    while True:
        try:
            if is_auto_renew_enabled():
                await process_auto_renewals(bot)
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Планировщик автопродления остановлен")
            break
        except Exception as e:
            logger.error(f"Ошибка автопродления: {e}")
            await asyncio.sleep(3600)
