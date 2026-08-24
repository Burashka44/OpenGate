import sqlite3
import logging
import secrets
import string
import datetime
from typing import Optional, List, Dict, Any, Tuple
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'get_setting',
    'set_setting',
    'delete_setting',
    'get_cardlink_partner_uuid',
    'set_cardlink_partner_uuid',
    'is_crypto_enabled',
    'is_stars_enabled',
    'is_crypto_configured',
    'is_cards_enabled',
    'is_cards_configured',
    'is_yookassa_qr_enabled',
    'is_yookassa_qr_configured',
    'get_yookassa_credentials',
    'is_wata_enabled',
    'is_wata_configured',
    'get_wata_token',
    'is_platega_enabled',
    'is_platega_configured',
    'get_platega_credentials',
    'is_cardlink_enabled',
    'is_cardlink_configured',
    'get_cardlink_credentials',
    'is_trial_enabled',
    'get_trial_tariff_id',
    'is_demo_payment_enabled',
    'is_cryptobot_enabled',
    'is_cryptobot_configured',
    'get_cryptobot_token',
    'is_heleket_enabled',
    'is_heleket_configured',
    'get_heleket_credentials',
    'is_maintenance_mode',
    'set_maintenance_mode',
    'is_web_enabled',
    'get_web_port',
    'get_web_public_url',
    'is_auto_renew_enabled',
    'get_trial_channel',
    'get_admin_alerts_chat_id',
]

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Получает значение настройки.
    
    Args:
        key: Ключ настройки
        default: Значение по умолчанию
        
    Returns:
        Значение настройки или default
    """
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        )
        row = cursor.fetchone()
        return row['value'] if row else default

def set_setting(key: str, value: str) -> None:
    """
    Устанавливает значение настройки.
    
    Args:
        key: Ключ настройки
        value: Значение настройки
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        logger.info(f"Настройка обновлена: {key}")

def delete_setting(key: str) -> bool:
    """
    Удаляет настройку.
    
    Args:
        key: Ключ настройки
        
    Returns:
        True если настройка была удалена
    """
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return cursor.rowcount > 0


CARDLINK_PARTNER_UUID_SETTING = 'cardlink_partner_uuid'


def get_cardlink_partner_uuid() -> str:
    """Опциональный partner UUID Cardlink (ваша партнёрская программа)."""
    return get_setting(CARDLINK_PARTNER_UUID_SETTING, '') or ''


def set_cardlink_partner_uuid(partner_uuid: str) -> None:
    """Сохраняет partner UUID Cardlink."""
    set_setting(CARDLINK_PARTNER_UUID_SETTING, partner_uuid.strip())

def is_crypto_enabled() -> bool:
    """Проверяет, включены ли крипто-платежи."""
    return get_setting('crypto_enabled', '0') == '1'

def is_stars_enabled() -> bool:
    """Проверяет, включены ли Telegram Stars."""
    return get_setting('stars_enabled', '0') == '1'

def is_crypto_configured() -> bool:
    """
    Проверяет, настроены ли крипто-платежи полностью.
    
    Returns:
        True если крипто включены И есть ссылка на товар (для стандартного режима) или просто включены
    """
    if not is_crypto_enabled():
        return False
    crypto_item_url = get_setting('crypto_item_url')
    return bool(crypto_item_url and crypto_item_url.strip())



def is_cards_enabled() -> bool:
    """Проверяет, включена ли оплата картами (ЮКасса)."""
    return get_setting('cards_enabled', '0') == '1'

def is_cards_configured() -> bool:
    """
    Проверяет, настроена ли оплата картами.
    
    Returns:
        True если оплата картами включена И есть provider_token
    """
    if not is_cards_enabled():
        return False
    token = get_setting('cards_provider_token')
    return bool(token and token.strip())

def is_yookassa_qr_enabled() -> bool:
    """Проверяет, включена ли QR-оплата через ЮКассу."""
    return get_setting('yookassa_qr_enabled', '0') == '1'

def is_yookassa_qr_configured() -> bool:
    """
    Проверяет, настроена ли QR-оплата через ЮКассу полностью.

    Returns:
        True если QR включена И есть shop_id и secret_key
    """
    if not is_yookassa_qr_enabled():
        return False
    shop_id = get_setting('yookassa_shop_id', '')
    secret_key = get_setting('yookassa_secret_key', '')
    return bool(shop_id and shop_id.strip() and secret_key and secret_key.strip())

def get_yookassa_credentials() -> tuple[str, str]:
    """
    Возвращает учётные данные ЮКасса для прямого API.

    Returns:
        Кортеж (shop_id, secret_key)
    """
    shop_id = get_setting('yookassa_shop_id', '')
    secret_key = get_setting('yookassa_secret_key', '')
    return shop_id, secret_key

def is_wata_enabled() -> bool:
    """Проверяет, включена ли оплата через WATA."""
    return get_setting('wata_enabled', '0') == '1'

def is_wata_configured() -> bool:
    """
    Проверяет, настроена ли оплата через WATA полностью.

    Returns:
        True если WATA включена И задан JWT-токен
    """
    if not is_wata_enabled():
        return False
    token = get_setting('wata_jwt_token', '')
    return bool(token and token.strip())

def get_wata_token() -> str:
    """
    Возвращает JWT-токен для WATA API.

    Returns:
        Строка с JWT-токеном (или пустая строка)
    """
    return get_setting('wata_jwt_token', '') or ''

def is_platega_enabled() -> bool:
    """Проверяет, включена ли оплата через Platega."""
    return get_setting('platega_enabled', '0') == '1'

def is_platega_configured() -> bool:
    """
    Проверяет, настроена ли оплата через Platega полностью.

    Returns:
        True если Platega включена И заданы merchant_id и secret
    """
    if not is_platega_enabled():
        return False
    merchant_id = get_setting('platega_merchant_id', '')
    secret = get_setting('platega_secret', '')
    return bool(merchant_id and merchant_id.strip() and secret and secret.strip())

def get_platega_credentials() -> tuple[str, str]:
    """
    Возвращает учётные данные Platega для прямого API.

    Returns:
        Кортеж (merchant_id, secret)
    """
    merchant_id = get_setting('platega_merchant_id', '')
    secret = get_setting('platega_secret', '')
    return merchant_id, secret

def is_cardlink_enabled() -> bool:
    """Проверяет, включена ли оплата через Cardlink."""
    return get_setting('cardlink_enabled', '0') == '1'

def is_cardlink_configured() -> bool:
    """
    Проверяет, настроена ли оплата через Cardlink полностью.

    Returns:
        True если Cardlink включён И заданы shop_id и api_token
    """
    if not is_cardlink_enabled():
        return False
    shop_id = get_setting('cardlink_shop_id', '')
    token = get_setting('cardlink_api_token', '')
    return bool(shop_id and shop_id.strip() and token and token.strip())

def get_cardlink_credentials() -> tuple[str, str]:
    """
    Возвращает учётные данные Cardlink для прямого API.

    Returns:
        Кортеж (shop_id, api_token)
    """
    shop_id = get_setting('cardlink_shop_id', '')
    token = get_setting('cardlink_api_token', '')
    return shop_id, token

def is_trial_enabled() -> bool:
    """Включена ли функция пробной подписки."""
    return get_setting('trial_enabled', '0') == '1'

def get_trial_tariff_id() -> Optional[int]:
    """
    Возвращает ID тарифа для пробной подписки.
    
    Returns:
        ID тарифа или None если тариф не задан
    """
    val = get_setting('trial_tariff_id', '')
    return int(val) if val and val.isdigit() else None

def is_demo_payment_enabled() -> bool:
    """Включена ли демонстрационная оплата РФ картой."""
    return get_setting('demo_payment_enabled', '0') == '1'


# ============================================================================
# CryptoBot (Crypto Pay API) / Heleket
# ============================================================================

def is_cryptobot_enabled() -> bool:
    """Включена ли оплата через CryptoBot (Crypto Pay API)."""
    return get_setting('cryptobot_enabled', '0') == '1'

def is_cryptobot_configured() -> bool:
    """True если CryptoBot включён и задан API-токен приложения Crypto Pay."""
    if not is_cryptobot_enabled():
        return False
    token = get_setting('cryptobot_token', '')
    return bool(token and token.strip())

def get_cryptobot_token() -> str:
    """API-токен приложения Crypto Pay (@CryptoBot → Crypto Pay → Create App)."""
    return get_setting('cryptobot_token', '') or ''

def is_heleket_enabled() -> bool:
    """Включена ли оплата через Heleket."""
    return get_setting('heleket_enabled', '0') == '1'

def is_heleket_configured() -> bool:
    """True если Heleket включён и заданы merchant_id + api_key."""
    if not is_heleket_enabled():
        return False
    merchant_id = get_setting('heleket_merchant_id', '')
    api_key = get_setting('heleket_api_key', '')
    return bool(merchant_id and merchant_id.strip() and api_key and api_key.strip())

def get_heleket_credentials() -> tuple[str, str]:
    """Кортеж (merchant_id, api_key) для Heleket API."""
    return (
        get_setting('heleket_merchant_id', '') or '',
        get_setting('heleket_api_key', '') or '',
    )


# ============================================================================
# Веб-сервер / maintenance / алерты / trial-гейт
# ============================================================================

def is_maintenance_mode() -> bool:
    """Режим обслуживания: продажи на паузе."""
    return get_setting('maintenance_mode', '0') == '1'

def set_maintenance_mode(enabled: bool) -> None:
    """Включает/выключает режим обслуживания."""
    set_setting('maintenance_mode', '1' if enabled else '0')

def is_web_enabled() -> bool:
    """Включён ли встроенный веб-сервер (sub page, вебхуки, Mini App)."""
    return get_setting('web_enabled', '0') == '1'

def get_web_port() -> int:
    """Локальный порт веб-сервера."""
    try:
        return int(get_setting('web_port', '8081') or '8081')
    except ValueError:
        return 8081

def get_web_public_url() -> str:
    """Публичный URL веб-сервера (например https://sub.example.com), без слэша."""
    return (get_setting('web_public_url', '') or '').strip().rstrip('/')

def is_auto_renew_enabled() -> bool:
    """Глобальный тумблер автопродления с баланса."""
    return get_setting('auto_renew_enabled', '0') == '1'

def get_trial_channel() -> tuple[str, str]:
    """Кортеж (channel_id, channel_link) для гейта пробной подписки."""
    return (
        (get_setting('trial_channel_id', '') or '').strip(),
        (get_setting('trial_channel_link', '') or '').strip(),
    )

def get_admin_alerts_chat_id() -> str:
    """Чат для алертов (пусто = слать админам в ЛС)."""
    return (get_setting('admin_alerts_chat_id', '') or '').strip()
