"""
Сервис биллинга — обработка платежей.

Проверка подписей, создание/продление ключей после оплаты.
Создание QR-платежей через ЮКасса REST API.
Реферальные начисления.
"""
import hmac
import hashlib
import logging
import uuid
import base64
import aiohttp
import qrcode
import io
import math
from typing import Optional, Dict, Any, Tuple

from database.requests import (
    find_order_by_order_id, complete_order, is_order_already_paid,
    get_vpn_key_by_id, extend_vpn_key, get_setting,
    get_yookassa_credentials, get_wata_token, get_platega_credentials,
    get_cardlink_credentials, get_cardlink_partner_uuid,
    is_referral_enabled, get_referral_reward_type, get_active_referral_levels,
    get_user_referrer, get_user_referral_coefficient, get_user_balance,
    add_to_balance, deduct_from_balance, add_days_to_first_active_key,
    update_referral_stat
)
from bot.services.exchange_rate import get_usd_rub_rate

logger = logging.getLogger(__name__)

STAR_TO_USD = 0.013
USDT_TO_USD = 1.0

YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"
WATA_API_URL = "https://api.wata.pro/api/h2h"
PLATEGA_API_URL = "https://app.platega.io"
PLATEGA_PAYMENT_METHOD_SBP = 2
CARDLINK_API_URL = "https://cardlink.link"

# Алфавит для Base62 кодирования
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"




def encode_base62(data: bytes) -> str:
    """
    Кодирует бинарные данные в Base62.
    
    Используется для формирования подписи callback криптопроцессора (bill*-HMAC).
    
    Args:
        data: Бинарные данные
        
    Returns:
        Строка в формате Base62
    """
    if not data:
        return ""
    
    num = int.from_bytes(data, 'big')
    if num == 0:
        return "0"
    
    res = []
    while num > 0:
        num, rem = divmod(num, 62)
        res.append(ALPHABET[rem])
    
    return "".join(reversed(res))


def verify_crypto_signature(data_part: str, received_signature: str, secret_key: str) -> bool:
    """
    Проверяет подпись callback от криптопроцессинга.
    
    Подпись = Base62(HMAC-SHA256(data_part, secret_key)[:11]).
    
    Алгоритм:
    1. Вычисляем HMAC-SHA256 от data_part с секретным ключом
    2. Берем первые 11 байт бинарного результата
    3. Кодируем в Base62
    
    Args:
        data_part: Все сегменты кроме последнего (например bill1-aZ1-bY-1-_-1000)
        received_signature: Полученная подпись (последний сегмент)
        secret_key: Секретный ключ продавца
        
    Returns:
        True если подпись валидна
    """
    # Вычисляем HMAC-SHA256
    h = hmac.new(
        secret_key.encode('utf-8'),
        data_part.encode('utf-8'),
        hashlib.sha256
    ).digest()
    
    # Берем первые 11 байт и кодируем в Base62
    truncated = h[:11]
    expected = encode_base62(truncated)
    
    # Сравниваем подписи
    is_valid = hmac.compare_digest(expected, received_signature)
    
    if not is_valid:
        logger.warning("Неверная подпись crypto callback (HMAC mismatch)")
    
    return is_valid


def parse_crypto_callback(start_param: str) -> Optional[Dict[str, Any]]:
    """
    Парсит параметр start из callback криптопроцессинга.
    
    Формат: bill1-ORDER_ID-ITEM_ID-TARIFF-PROMO-PRICE-SIGNATURE
    
    Args:
        start_param: Значение параметра start из deep link
        
    Returns:
        Словарь с полями: order_id, item_id, tariff, promo, price, signature, data_part
        или None если формат неверный
    """
    if not start_param or not start_param.startswith('bill'):
        return None
    
    parts = start_param.split('-')
    
    # Минимум: bill1-ORDER_ID-ITEM_ID-TARIFF-PROMO-PRICE-SIGNATURE (7 частей)
    if len(parts) < 7:
        logger.warning(f"Неверный формат callback: {start_param} (частей: {len(parts)})")
        return None
    
    try:
        # Последняя часть — подпись
        signature = parts[-1]
        # Остальное — данные для проверки подписи
        data_part = start_param.rsplit('-', 1)[0]
        
        return {
            'prefix': parts[0],        # bill1 или bill0
            'order_id': parts[1],      # наш invoice_id
            'item_id': parts[2],       # ID товара в Ya.Seller
            'tariff': parts[3],        # номер тарифа (1-9) или '_'
            'promo': parts[4],         # промокод или '_'
            'price': int(parts[5]) if parts[5] != '_' else 0,  # цена в центах
            'signature': signature,
            'data_part': data_part
        }
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка парсинга callback: {e}")
        return None


async def process_payment_order(order_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Универсальная обработка успешного ордера (Crypto или Stars).
    Закрывает ордер, продлевает ключ или создаёт черновик.
    
    Returns:
        (success, message_text, order_data)
    """
    from database.requests import (
        is_order_already_paid, find_order_by_order_id, complete_order, 
        extend_vpn_key, create_initial_vpn_key, update_payment_key_id
    )
    
    # 1. Проверка на дубликат (на всякий случай, если вызывающий не проверил)
    if is_order_already_paid(order_id):
        # Получаем ордер чтобы вернуть контекст
        order = find_order_by_order_id(order_id)
        return True, "✅ Этот платёж уже был обработан ранее.", order

    # 2. Поиск ордера
    order = find_order_by_order_id(order_id)
    if not order:
        logger.warning(f"Ордер не найден: {order_id}")
        return False, "⚠️ Ордер не найден. Обратитесь в поддержку.", None
    
    # 3. Закрываем ордер — атомарный шлюз идемпотентности.
    # UPDATE ... WHERE status='pending' выполнит выдачу ровно один раз:
    # проигравший гонку поток увидит status='paid' и НЕ продлит ключ повторно.
    if not complete_order(order_id):
        fresh = find_order_by_order_id(order_id)
        if fresh and fresh['status'] == 'paid':
            logger.info(f"Order {order_id}: параллельная обработка, выдача уже выполнена")
            return True, "✅ Этот платёж уже был обработан ранее.", fresh
        return False, "❌ Ошибка обновления статуса платежа.", order
    
    logger.info(f"Order {order_id} processed (paid)")

    user_internal_id = order['user_id']
    days = order.get('period_days') or order.get('duration_days') or 30

    # --- Пополнение баланса (purpose='topup') ---
    if order.get('purpose') == 'topup':
        from database.requests import add_to_balance, mark_order_fulfilled
        from bot.services.user_locks import user_locks
        amount = order.get('amount_cents', 0) or 0
        if amount > 0:
            async with user_locks[user_internal_id]:
                add_to_balance(user_internal_id, amount)
        mark_order_fulfilled(order_id)
        logger.info(f"Topup order {order_id}: +{amount} коп на баланс user {user_internal_id}")
        return True, (
            f"✅ Оплата прошла успешно!\n\n"
            f"💎 Баланс пополнен на <b>{amount / 100:g} ₽</b>."
        ), order

    if order['vpn_key_id']:
        if days and extend_vpn_key(order['vpn_key_id'], days):
            logger.info(f"Ключ {order['vpn_key_id']} продлён на {days} дней (order={order_id})")
            
            from bot.services.vpn_api import (
                push_key_to_panel, restore_traffic_limit_in_db,
                ensure_subscription_keys_on_server,
            )
            from database.requests import get_vpn_key_by_id
            # Восстанавливаем лимит трафика в БД (без обращения к панели)
            restore_traffic_limit_in_db(order['vpn_key_id'])
            # Пушим ВСЕ данные из БД на панель одним вызовом (сброс up/down + обновление)
            await push_key_to_panel(order['vpn_key_id'], reset_traffic=True)
            # Subscription: зеркалим обновлённые totalGB/expiryTime/enable на все inbound
            _renewed_key = get_vpn_key_by_id(order['vpn_key_id'])
            if _renewed_key and _renewed_key.get('sub_id'):
                try:
                    await ensure_subscription_keys_on_server(order['vpn_key_id'])
                except Exception as _e:
                    logger.warning(
                        f"renew: ensure_subscription_keys_on_server({order['vpn_key_id']}) "
                        f"не удался: {_e}"
                    )

            # Рефералка — через pay_referral_once (check/webhook), не здесь
            from database.requests import mark_order_fulfilled, consume_user_discount
            mark_order_fulfilled(order_id)
            consume_user_discount(user_internal_id)
            return True, f"✅ Оплата прошла успешно!\n\nВаш ключ продлён на {days} дней.", order
        else:
            logger.error(f"Не удалось продлить ключ {order['vpn_key_id']} после оплаты!")
            return True, "✅ Оплата принята!\n\n⚠️ Возникла проблема с продлением. Мы разберёмся.", order
    else:
        if not order.get('tariff_id'):
            logger.error(f"Ордер {order_id}: тариф не найден или неактивен в БД (received tariff_id could not be resolved).")
            from bot.errors import TariffNotFoundError
            raise TariffNotFoundError()
        
        try:
            days = order.get('period_days') or order.get('duration_days') or 30
            # Получаем лимит трафика из тарифа
            from database.requests import get_tariff_by_id as _get_tariff
            _tariff = _get_tariff(order['tariff_id'])
            traffic_limit_bytes = (_tariff.get('traffic_limit_gb', 0) or 0) * (1024**3) if _tariff else 0
            key_id = create_initial_vpn_key(order['user_id'], order['tariff_id'], days, traffic_limit=traffic_limit_bytes)
            
            update_payment_key_id(order_id, key_id)
            order['vpn_key_id'] = key_id
            
            logger.info(f"Создан черновик ключа {key_id} для заказа {order_id}")
            
            # Рефералка — через pay_referral_once (check/webhook), не здесь
            from database.requests import mark_order_fulfilled, consume_user_discount
            mark_order_fulfilled(order_id)
            consume_user_discount(user_internal_id)
            return True, "✅ Оплата прошла успешно!", order
            
        except Exception as e:
            logger.error(f"Ошибка создания черновика ключа: {e}")
            return True, "✅ Оплата принята, но произошла ошибка при создании ключа. Обратитесь в поддержку.", order


async def fulfill_paid_order(order_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Идемпотентная выдача: process_payment_order + списание balance_to_deduct с ордера.
    balance_to_deduct списывается атомарно (claim) — без двойного списания при retry/webhook.
    """
    from database.requests import (
        find_order_by_order_id, deduct_from_balance, claim_balance_to_deduct,
    )
    from bot.services.user_locks import user_locks

    success, text, order = await process_payment_order(order_id)
    if not (success and order):
        return success, text, order

    bal = claim_balance_to_deduct(order_id)
    if bal > 0:
        user_id = order['user_id']
        async with user_locks[user_id]:
            if not deduct_from_balance(user_id, bal):
                logger.error(
                    f"Order {order_id}: не удалось списать balance_to_deduct={bal} "
                    f"после выдачи — требуется ручная проверка"
                )
            else:
                logger.info(f"Order {order_id}: списано balance_to_deduct={bal} коп")

    fresh = find_order_by_order_id(order_id) or order
    return True, text, fresh


def referral_amount_from_order(order: Dict[str, Any]) -> Tuple[int, str]:
    """
    Сумма для рефералки из полей ордера (не из полного тарифа).

    Returns:
        (amount_raw, payment_type_for_convert)
    """
    ptype = order.get('payment_type') or ''
    if ptype == 'stars':
        return int(order.get('amount_stars') or 0), 'stars'
    if ptype == 'crypto':
        return int(order.get('amount_cents') or 0), 'crypto'
    if ptype == 'balance':
        return 0, 'balance'
    # RUB-провайдеры + partial: amount_cents = оплаченная часть;
    # рефералка — по полной стоимости = amount_cents + balance_to_deduct
    paid = int(order.get('amount_cents') or 0)
    bal = int(order.get('balance_to_deduct') or 0)
    return paid + bal, ptype or 'cards'


async def pay_referral_once(order: Dict[str, Any]) -> None:
    """Начисляет рефералку один раз (по флагу referral_paid_at)."""
    from database.requests import mark_referral_paid, find_order_by_order_id

    order_id = order.get('order_id')
    if not order_id:
        return
    if order.get('referral_paid_at'):
        return
    if (order.get('payment_type') or '') == 'balance':
        mark_referral_paid(order_id)
        return

    if not mark_referral_paid(order_id):
        return  # уже начислено параллельно

    amount_raw, ptype = referral_amount_from_order(order)
    days = order.get('period_days') or order.get('duration_days') or 30
    if amount_raw <= 0:
        return
    try:
        await process_referral_reward(order['user_id'], days, amount_raw, ptype)
    except Exception as e:
        logger.error(f"pay_referral_once({order_id}) failed: {e}")


async def notify_payment_once(bot, order: Dict[str, Any], text: str) -> None:
    """
    Уведомляет пользователя в ЛС один раз.
    Для draft: кнопки «Мои ключи» / key_configure — без запуска FSM config.
    """
    from database.requests import mark_user_notified, get_user_by_id, find_order_by_order_id
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    order_id = order.get('order_id')
    if not order_id:
        return
    fresh = find_order_by_order_id(order_id) or order
    if fresh.get('user_notified_at'):
        return
    if not mark_user_notified(order_id):
        return

    user = get_user_by_id(fresh['user_id'])
    if not user:
        return

    key_id = fresh.get('vpn_key_id')
    rows = [[InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")]]
    if key_id and not fresh.get('purpose') == 'topup':
        # Новый ключ (черновик) — конфиг только из user handler
        from database.requests import get_vpn_key_by_id
        key = get_vpn_key_by_id(key_id) if key_id else None
        if key and not key.get('host_server_id') and not key.get('client_uuid'):
            rows.insert(0, [
                InlineKeyboardButton(
                    text="⚙️ Настроить ключ",
                    callback_data=f"key_configure:{key_id}",
                )
            ])
    rows.append([InlineKeyboardButton(text="🈴 На главную", callback_data="start")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await bot.send_message(
            chat_id=user['telegram_id'], text=text, reply_markup=kb, parse_mode='HTML'
        )
    except Exception as e:
        logger.warning(f"notify_payment_once({order_id}): {e}")


async def process_crypto_payment(start_param: str, user_id: Optional[int] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Обрабатывает платёж от криптопроцессинга (parse + verify + confirm).
    """
    # Парсим callback
    parsed = parse_crypto_callback(start_param)
    if not parsed:
        return False, "❌ Неверный формат платёжных данных", None
    
    # Получаем секретный ключ
    secret_key = get_setting('crypto_secret_key')
    if not secret_key:
        logger.error("Секретный ключ криптопроцессинга не настроен!")
        return False, "❌ Ошибка конфигурации. Обратитесь в поддержку.", None
    
    # Проверяем подпись
    if not verify_crypto_signature(parsed['data_part'], parsed['signature'], secret_key):
        return False, "❌ Неверная подпись платежа. Попробуйте снова.", None
    
    order_id = parsed['order_id']
    
    # --- ЛОГИКА ОБРАБОТКИ ОРДЕРОВ (Внешние/Внутренние) ---
    is_internal_order = order_id.startswith("00")
    order = find_order_by_order_id(order_id)
    
    if order:
        # Сверяем сумму платежа с фактически выставленной в ордере (со скидкой)
        expected_cents = int(order.get('amount_cents') or 0)
        received_cents = parsed.get('price', 0)
        if expected_cents > 0 and received_cents < expected_cents:
            logger.error(
                f"Ордер {order_id}: Сумма платежа недостаточна. "
                f"Ожидалось {expected_cents}, получено {received_cents}"
            )
            return False, "❌ Сумма платежа не совпадает с тарифом.", None
    
    if not order:
        if is_internal_order:
             return False, "❌ Ордер не найден в системе.", None
        
        # Внешний ордер -> Создаем PAID order в базе ПЕРЕД обработкой
        if not user_id:
             return False, "⚠️ Ошибка обработки внешнего заказа (нет user_id).", None
        
        logger.info(f"Новый внешний ордер: {order_id}")
        
        # Внешний ордер без тарифа — ошибка
        logger.error(f"Внешний ордер {order_id} без привязки к тарифу!")
        from bot.errors import TariffNotFoundError
        raise TariffNotFoundError()
    
    # Delegate to unified logic
    success, text, order = await fulfill_paid_order(order_id)
    if success and order:
        await pay_referral_once(order)
    return success, text, order


def get_crypto_processor_base_url() -> str:
    """Базовый URL крипто-бота: setting crypto_processor_url или префикс crypto_item_url."""
    base = (get_setting('crypto_processor_url', '') or '').strip().rstrip('/')
    if base:
        return base
    item_url = get_setting('crypto_item_url', '') or ''
    if '?start=' in item_url:
        return item_url.split('?start=')[0].rstrip('/')
    return ''


def build_crypto_payment_url(
    item_id: str,
    invoice_id: str,
    price_cents: Optional[int] = None
) -> str:
    """
    Формирует ссылку на криптопроцессинг с нашим invoice.
    
    Формат: {processor_base}?start=item-{item_id}-{ref}-{promo}-{invoice}-{price}
    
    Args:
        item_id: ID товара в процессоре (из настроек)
        invoice_id: Наш уникальный invoice (макс 8 символов)
        price_cents: Цена в центах (если нужно переопределить)
        
    Returns:
        URL для перехода в криптопроцессинг или пустая строка
    """
    base = get_crypto_processor_base_url()
    if not base or not item_id:
        return ''
    # Формат: item-{item_id}-{ref_code}-{promo}-{invoice}-{price}
    # Пустые параметры заменяем прочерками
    
    ref_code = ""  # Реффералку не используем
    promo = ""     # Промокод не используем
    
    parts = [
        "item",
        item_id,
        ref_code,
        promo,
        invoice_id
    ]
    
    # Добавляем цену если нужно зафиксировать
    if price_cents:
        parts.append(str(price_cents))
    
    start_param = "-".join(parts)
    
    return f"{base}?start={start_param}"


def extract_item_id_from_url(crypto_item_url: str) -> Optional[str]:
    """
    Извлекает item_id из ссылки на товар крипто-процессора.
    
    Формат ссылки: ...?start=item-{item_id}...
    
    Args:
        crypto_item_url: Полная ссылка на товар
        
    Returns:
        item_id или None
    """
    if not crypto_item_url:
        return None
    
    # Ищем start= параметр
    if '?start=' in crypto_item_url:
        start_param = crypto_item_url.split('?start=')[1]
        parts = start_param.split('-')
        if len(parts) >= 2 and parts[0] == 'item':
            return parts[1]
    
    return None


# ============================================================================
# ЮКАССА QR-ОПЛАТА (прямой REST API без Telegram Payments)
# ============================================================================

async def create_yookassa_qr_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Создаёт платёж в ЮКасса REST API с подтверждением через QR-код.

    Возвращает изображение QR-кода (PNG) по ссылке, которую можно
    отправить пользователю прямо в Telegram как фото.

    Args:
        amount_rub: Сумма в рублях (например, 299.00)
        order_id: Наш внутренний ордер (для metadata)
        description: Описание платежа (показывается в форме оплаты)
        metadata: Дополнительные метаданные (необязательно)

    Returns:
        Словарь с ключами:
            - yookassa_payment_id: ID платежа в системе ЮКасса
            - qr_image_url: URL изображения QR-кода (PNG)
            - qr_url: Ссылка, зашитая в QR (для открытия в браузере)

    Raises:
        ValueError: Если учётные данные не настроены
        aiohttp.ClientError: Если API недоступен
        RuntimeError: Если API вернул ошибку
    """
    shop_id, secret_key = get_yookassa_credentials()
    if not shop_id or not secret_key:
        raise ValueError("ЮКасса: не настроены shop_id или secret_key")

    # Заголовок Basic Auth: base64(shop_id:secret_key)
    credentials = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()

    # Ключ идемпотентности — уникальный для этого ордера
    idempotence_key = f"qr-{order_id}-{uuid.uuid4().hex[:8]}"

    payload = {
        "amount": {
            "value": f"{amount_rub:.2f}",
            "currency": "RUB"
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": "https://t.me"
        },
        "description": description,
        "receipt": {
            "customer": {
                "email": f"user_{order_id}@t.me"
            },
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": {
                        "value": f"{amount_rub:.2f}",
                        "currency": "RUB"
                    },
                    "vat_code": 1,
                    "payment_mode": "full_prepayment",
                    "payment_subject": "service"
                }
            ]
        },
        "metadata": {
            "order_id": order_id,
            **(metadata or {})
        }
    }

    headers = {
        "Authorization": f"Basic {credentials}",
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            YOOKASSA_API_URL,
            json=payload,
            headers=headers
        ) as response:
            data = await response.json()

            if response.status not in (200, 201):
                error_desc = data.get('description', 'Неизвестная ошибка')
                logger.error(f"ЮКасса API ошибка {response.status}: {error_desc} | payload={payload}")
                raise RuntimeError(f"ЮКасса API ошибка: {error_desc}")

            confirmation = data.get('confirmation', {})
            qr_url = confirmation.get('confirmation_url', '')
            
            if not qr_url:
                logger.error(f"ЮКасса API не вернул confirmation_url: {data}")
                raise RuntimeError("ЮКасса API не вернул данные для QR-кода")

            # Генерируем QR-код из строки оплаты через локальную библиотеку qrcode
            
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, format="PNG")
            qr_image_data = bio.getvalue()

            logger.info(
                f"ЮКасса QR создан: payment_id={data['id']}, order_id={order_id}, "
                f"amount={amount_rub} RUB"
            )

            return {
                'yookassa_payment_id': data['id'],
                'qr_image_data': qr_image_data,
                'qr_url': qr_url,
                'status': data.get('status', 'pending')
            }


async def check_yookassa_payment_status(yookassa_payment_id: str) -> str:
    """
    Проверяет статус платежа в ЮКасса REST API.

    Args:
        yookassa_payment_id: ID платежа в системе ЮКасса

    Returns:
        Строка статуса: 'pending', 'waiting_for_capture', 'succeeded', 'canceled'

    Raises:
        ValueError: Если учётные данные не настроены
        aiohttp.ClientError: Если API недоступен
        RuntimeError: Если API вернул ошибку
    """
    shop_id, secret_key = get_yookassa_credentials()
    if not shop_id or not secret_key:
        raise ValueError("ЮКасса: не настроены shop_id или secret_key")

    credentials = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json"
    }

    url = f"{YOOKASSA_API_URL}/{yookassa_payment_id}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            data = await response.json()

            if response.status != 200:
                error_desc = data.get('description', 'Неизвестная ошибка')
                logger.error(f"ЮКасса статус ошибка {response.status}: {error_desc}")
                raise RuntimeError(f"ЮКасса API ошибка: {error_desc}")

            status = data.get('status', 'pending')
            logger.debug(f"ЮКасса payment {yookassa_payment_id}: status={status}")
            return status


# ============================================================================
# WATA — оплата картой/СБП через REST API (https://wata.pro/api)
# ============================================================================

async def create_wata_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str
) -> Dict[str, Any]:
    """
    Создаёт платёжную ссылку в WATA через H2H API.

    POST https://api.wata.pro/api/h2h/links/

    Args:
        amount_rub: Сумма в рублях
        order_id: Наш внутренний order_id
        description: Описание платежа
        bot_name: Username бота (для построения successRedirectUrl)

    Returns:
        Словарь с ключами:
            - wata_link_id: ID ссылки в системе WATA
            - qr_image_data: PNG-байты QR-кода
            - qr_url: Ссылка для оплаты (карты/СБП)
            - status: Статус платежа

    Raises:
        ValueError: Если JWT-токен не настроен
        RuntimeError: Если API вернул ошибку
    """
    token = get_wata_token()
    if not token:
        raise ValueError("WATA: JWT-токен не настроен")

    return_url = f"https://t.me/{bot_name}" if bot_name else "https://t.me"

    payload = {
        "amount": round(float(amount_rub), 2),
        "currency": "RUB",
        "description": description[:255],
        "orderId": order_id,
        "successRedirectUrl": return_url,
        "failRedirectUrl": return_url,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    url = f"{WATA_API_URL}/links/"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            try:
                data = await response.json()
            except Exception:
                text = await response.text()
                logger.error(f"WATA API: невозможно разобрать ответ ({response.status}): {text}")
                raise RuntimeError("WATA API вернул некорректный ответ")

            if response.status not in (200, 201):
                error_desc = data.get('error') or data.get('message') or data.get('description') or 'Неизвестная ошибка'
                logger.error(f"WATA API ошибка {response.status}: {error_desc} | payload={payload}")
                raise RuntimeError(f"WATA API ошибка: {error_desc}")

            wata_link_id = data.get('id') or data.get('linkId') or data.get('uuid')
            qr_url = data.get('url') or data.get('paymentUrl')

            if not wata_link_id or not qr_url:
                logger.error(f"WATA API не вернул id/url: {data}")
                raise RuntimeError("WATA API не вернул данные платёжной ссылки")

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, format="PNG")
            qr_image_data = bio.getvalue()

            logger.info(
                f"WATA ссылка создана: link_id={wata_link_id}, order_id={order_id}, "
                f"amount={amount_rub} RUB"
            )

            return {
                'wata_link_id': str(wata_link_id),
                'qr_image_data': qr_image_data,
                'qr_url': qr_url,
                'status': str(data.get('status', 'Created')).lower(),
            }


async def check_wata_payment_status(order_id: str) -> str:
    """
    Проверяет статус платежа WATA по нашему order_id.

    GET https://api.wata.pro/api/h2h/transactions/?orderId={order_id}

    WATA имеет лимит — не чаще одного запроса в 30 секунд по одному order_id.
    Контроль частоты запросов выполняется на стороне обработчика.

    Args:
        order_id: Наш внутренний order_id

    Returns:
        Нормализованный статус: 'pending' | 'succeeded' | 'canceled'

    Raises:
        ValueError: Если JWT-токен не настроен
        RuntimeError: Если API вернул ошибку
    """
    token = get_wata_token()
    if not token:
        raise ValueError("WATA: JWT-токен не настроен")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    url = f"{WATA_API_URL}/transactions/"
    params = {"orderId": order_id}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers) as response:
            try:
                data = await response.json()
            except Exception:
                text = await response.text()
                logger.error(f"WATA статус: невозможно разобрать ответ ({response.status}): {text}")
                raise RuntimeError("WATA API вернул некорректный ответ")

            if response.status != 200:
                error_desc = data.get('error') or data.get('message') or data.get('description') or 'Неизвестная ошибка'
                logger.error(f"WATA статус ошибка {response.status}: {error_desc}")
                raise RuntimeError(f"WATA API ошибка: {error_desc}")

            # WATA возвращает либо список транзакций, либо объект с items
            items = data if isinstance(data, list) else (data.get('items') or data.get('transactions') or [])

            if not items:
                return 'pending'

            # Если есть хоть одна Paid — считаем оплаченным
            statuses = [str(t.get('status', '')).lower() for t in items if isinstance(t, dict)]
            if any(s == 'paid' for s in statuses):
                return 'succeeded'
            if any(s == 'declined' for s in statuses) and not any(s in ('created', 'pending') for s in statuses):
                return 'canceled'

            return 'pending'


# ============================================================================
# PLATEGA — оплата СБП через REST API (https://app.platega.io)
# ============================================================================

async def create_platega_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str
) -> Dict[str, Any]:
    """
    Создаёт транзакцию в Platega API.

    POST https://app.platega.io/transaction/process

    Args:
        amount_rub: Сумма в рублях
        order_id: Наш внутренний order_id
        description: Описание платежа
        bot_name: Username бота (для построения returnUrl)

    Returns:
        Словарь с ключами:
            - platega_transaction_id: ID транзакции в системе Platega
            - qr_image_data: PNG-байты QR-кода
            - qr_url: Ссылка для оплаты (СБП)
            - status: Статус платежа

    Raises:
        ValueError: Если учётные данные не настроены
        RuntimeError: Если API вернул ошибку
    """
    merchant_id, secret = get_platega_credentials()
    if not merchant_id or not secret:
        raise ValueError("Platega: не настроены merchant_id или secret")

    return_url = f"https://t.me/{bot_name}" if bot_name else "https://t.me"
    fail_url = return_url

    # Platega требует id в формате UUID. Наш короткий order_id сохраняем в payload.
    transaction_uuid = str(uuid.uuid4())

    payload = {
        "paymentMethod": PLATEGA_PAYMENT_METHOD_SBP,
        "id": transaction_uuid,
        "paymentDetails": {
            "amount": round(float(amount_rub), 2),
            "currency": "RUB",
        },
        "description": description[:255],
        "returnUrl": return_url,
        "failedUrl": fail_url,
        "payload": order_id,
    }

    headers = {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    url = f"{PLATEGA_API_URL}/transaction/process"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            try:
                data = await response.json()
            except Exception:
                text = await response.text()
                logger.error(f"Platega API: невозможно разобрать ответ ({response.status}): {text}")
                raise RuntimeError("Platega API вернул некорректный ответ")

            if response.status not in (200, 201):
                error_desc = (
                    data.get('error') or data.get('message') or
                    data.get('description') or 'Неизвестная ошибка'
                )
                logger.error(f"Platega API ошибка {response.status}: {error_desc} | payload={payload}")
                raise RuntimeError(f"Platega API ошибка: {error_desc}")

            transaction_id = data.get('id') or data.get('transactionId') or data.get('uuid')
            qr_url = (
                data.get('redirect') or data.get('redirectUrl') or
                data.get('url') or data.get('paymentUrl')
            )

            if not transaction_id or not qr_url:
                logger.error(f"Platega API не вернул id/url: {data}")
                raise RuntimeError("Platega API не вернул данные платёжной ссылки")

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, format="PNG")
            qr_image_data = bio.getvalue()

            logger.info(
                f"Platega транзакция создана: id={transaction_id}, order_id={order_id}, "
                f"amount={amount_rub} RUB"
            )

            return {
                'platega_transaction_id': str(transaction_id),
                'qr_image_data': qr_image_data,
                'qr_url': qr_url,
                'status': str(data.get('status', 'PENDING')).upper(),
            }


async def check_platega_payment_status(transaction_id: str) -> str:
    """
    Проверяет статус транзакции Platega.

    GET https://app.platega.io/transaction/{transaction_id}

    Статусы Platega:
        - PENDING: в процессе оплаты
        - CONFIRMED: успешно оплачена
        - CANCELED: отменена
        - CHARGEBACKED: возвратная

    Args:
        transaction_id: ID транзакции в системе Platega

    Returns:
        Нормализованный статус: 'pending' | 'succeeded' | 'canceled'

    Raises:
        ValueError: Если учётные данные не настроены
        RuntimeError: Если API вернул ошибку
    """
    merchant_id, secret = get_platega_credentials()
    if not merchant_id or not secret:
        raise ValueError("Platega: не настроены merchant_id или secret")

    headers = {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Accept": "application/json",
    }

    url = f"{PLATEGA_API_URL}/transaction/{transaction_id}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            try:
                data = await response.json()
            except Exception:
                text = await response.text()
                logger.error(f"Platega статус: невозможно разобрать ответ ({response.status}): {text}")
                raise RuntimeError("Platega API вернул некорректный ответ")

            if response.status != 200:
                error_desc = (
                    data.get('error') or data.get('message') or
                    data.get('description') or 'Неизвестная ошибка'
                )
                logger.error(f"Platega статус ошибка {response.status}: {error_desc}")
                raise RuntimeError(f"Platega API ошибка: {error_desc}")

            status = str(data.get('status', '')).upper()
            logger.debug(f"Platega transaction {transaction_id}: status={status}")

            if status == 'CONFIRMED':
                return 'succeeded'
            if status in ('CANCELED', 'CANCELLED', 'CHARGEBACKED'):
                return 'canceled'
            return 'pending'


# ============================================================================
# CARDLINK — оплата Картой/СБП через REST API (https://cardlink.link)
# ============================================================================

async def create_cardlink_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str
) -> Dict[str, Any]:
    """
    Создаёт счёт (bill) в Cardlink API.

    POST https://cardlink.link/api/v1/bill/create

    Тело передаётся как application/x-www-form-urlencoded.
    Авторизация через Bearer token.

    Отличительная особенность: вместо webhook-а пользователь после оплаты
    возвращается в бота по deep-link `https://t.me/{bot}?start=cl_Success`
    (или cl_Fail / cl_Result), что триггерит ту же проверку, что и
    кнопка «✅ Я оплатил».

    Args:
        amount_rub: Сумма в рублях
        order_id: Наш внутренний order_id
        description: Описание платежа (не используется API, но логируется)
        bot_name: Username бота (для построения success_url/fail_url)

    Returns:
        Словарь с ключами:
            - cardlink_bill_id: ID счёта в системе Cardlink
            - qr_image_data: PNG-байты QR-кода
            - qr_url: Ссылка на страницу оплаты
            - status: Статус платежа

    Raises:
        ValueError: Если учётные данные не настроены
        RuntimeError: Если API вернул ошибку
    """
    shop_id, api_token = get_cardlink_credentials()
    if not shop_id or not api_token:
        raise ValueError("Cardlink: не настроены shop_id или api_token")

    form = aiohttp.FormData()
    form.add_field("shop_id", shop_id)
    form.add_field("amount", f"{float(amount_rub):.2f}")
    form.add_field("order_id", order_id)
    form.add_field("currency_in", "RUB")
    form.add_field("type", "normal")
    form.add_field("description", description[:255])
    form.add_field("name", description[:100])
    partner_uuid = get_cardlink_partner_uuid().strip()
    if partner_uuid:
        form.add_field("partner_uuid", partner_uuid)

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    url = f"{CARDLINK_API_URL}/api/v1/bill/create"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, headers=headers) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                text = await response.text()
                logger.error(f"Cardlink API: невозможно разобрать ответ ({response.status}): {text}")
                raise RuntimeError("Cardlink API вернул некорректный ответ")

            if response.status not in (200, 201):
                error_desc = 'Неизвестная ошибка'
                validation_details = ''
                if isinstance(data, dict):
                    err = data.get('error')
                    if isinstance(err, dict):
                        error_desc = err.get('description') or err.get('code') or error_desc
                    elif isinstance(err, str):
                        error_desc = err
                    error_desc = (
                        data.get('message')
                        or data.get('description')
                        or error_desc
                    )
                    validation = data.get('validation') or data.get('errors')
                    if validation:
                        validation_details = f" | validation={validation}"
                logger.error(
                    f"Cardlink API ошибка {response.status}: {error_desc} "
                    f"| order_id={order_id} | full_response={data}{validation_details}"
                )
                raise RuntimeError(f"Cardlink API ошибка: {error_desc}")

            # Ответ может быть вложен в поле 'success' (dict) или лежать в корне.
            # Если 'success' — это флаг (строка/bool), используем сам data.
            nested = data.get('success') if isinstance(data, dict) else None
            payload = nested if isinstance(nested, dict) else data

            bill_id = (
                payload.get('bill_id') or payload.get('id') or payload.get('uuid')
                if isinstance(payload, dict) else None
            )
            qr_url = (
                payload.get('link_page_url') or payload.get('url') or payload.get('payment_url')
                if isinstance(payload, dict) else None
            )

            if not bill_id or not qr_url:
                logger.error(f"Cardlink API не вернул bill_id/url: {data}")
                raise RuntimeError("Cardlink API не вернул данные платёжной ссылки")

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, format="PNG")
            qr_image_data = bio.getvalue()

            logger.info(
                f"Cardlink счёт создан: bill_id={bill_id}, order_id={order_id}, "
                f"amount={amount_rub} RUB"
            )

            return {
                'cardlink_bill_id': str(bill_id),
                'qr_image_data': qr_image_data,
                'qr_url': qr_url,
                'status': str(payload.get('status', 'NEW')).upper() if isinstance(payload, dict) else 'NEW',
            }


async def check_cardlink_payment_status(bill_id: str) -> str:
    """
    Проверяет статус счёта Cardlink.

    GET https://cardlink.link/api/v1/bill/status?id={bill_id}

    Статусы Cardlink:
        - NEW / PROCESS / UNDERPAID: в процессе
        - SUCCESS / OVERPAID: успешно оплачено
        - FAIL: отменён / неуспешный

    Args:
        bill_id: ID счёта в системе Cardlink

    Returns:
        Нормализованный статус: 'pending' | 'succeeded' | 'canceled'

    Raises:
        ValueError: Если учётные данные не настроены
        RuntimeError: Если API вернул ошибку
    """
    shop_id, api_token = get_cardlink_credentials()
    if not shop_id or not api_token:
        raise ValueError("Cardlink: не настроены shop_id или api_token")

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    url = f"{CARDLINK_API_URL}/api/v1/bill/status"
    params = {"id": bill_id}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                text = await response.text()
                logger.error(f"Cardlink статус: невозможно разобрать ответ ({response.status}): {text}")
                raise RuntimeError("Cardlink API вернул некорректный ответ")

            if response.status != 200:
                error_desc = (
                    (data.get('message') if isinstance(data, dict) else None) or
                    (data.get('error') if isinstance(data, dict) else None) or
                    'Неизвестная ошибка'
                )
                logger.error(f"Cardlink статус ошибка {response.status}: {error_desc}")
                raise RuntimeError(f"Cardlink API ошибка: {error_desc}")

            # Ответ может быть вложен в поле 'success' (dict) или лежать в корне.
            # Если 'success' — это флаг (строка/bool), используем сам data.
            nested = data.get('success') if isinstance(data, dict) else None
            payload = nested if isinstance(nested, dict) else data
            status = ''
            if isinstance(payload, dict):
                status = str(payload.get('status', '')).upper()

            logger.debug(f"Cardlink bill {bill_id}: status={status}")

            if status in ('SUCCESS', 'OVERPAID'):
                return 'succeeded'
            if status == 'FAIL':
                return 'canceled'
            return 'pending'


# ============================================================================
# CRYPTOBOT — Crypto Pay API (https://help.crypt.bot/crypto-pay-api)
# ============================================================================

CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"


async def create_cryptobot_invoice(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str
) -> Dict[str, Any]:
    """
    Создаёт инвойс в CryptoBot (Crypto Pay API) в фиатной привязке к RUB.

    POST https://pay.crypt.bot/api/createInvoice

    Пользователь платит в USDT/TON/BTC по курсу CryptoBot.

    Returns:
        Словарь с ключами:
            - provider_invoice_id: ID инвойса
            - pay_url: ссылка на оплату (t.me/CryptoBot?...)
            - qr_image_data: PNG-байты QR-кода
            - status: 'active'

    Raises:
        ValueError: Если токен не настроен
        RuntimeError: Если API вернул ошибку
    """
    from database.requests import get_cryptobot_token
    token = get_cryptobot_token()
    if not token:
        raise ValueError("CryptoBot: API-токен не настроен")

    payload = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": f"{float(amount_rub):.2f}",
        "description": description[:1024],
        "payload": order_id,
        "expires_in": 3600,
    }
    if bot_name:
        payload["paid_btn_name"] = "callback"
        payload["paid_btn_url"] = f"https://t.me/{bot_name}"

    headers = {"Crypto-Pay-API-Token": token}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{CRYPTOBOT_API_URL}/createInvoice", json=payload, headers=headers
        ) as response:
            data = await response.json(content_type=None)

            if not data.get("ok"):
                error = data.get("error", {})
                desc = error.get("name") or str(error) or "Неизвестная ошибка"
                logger.error(f"CryptoBot API ошибка: {desc} | payload={payload}")
                raise RuntimeError(f"CryptoBot API ошибка: {desc}")

            result = data["result"]
            invoice_id = result.get("invoice_id")
            pay_url = (
                result.get("bot_invoice_url")
                or result.get("mini_app_invoice_url")
                or result.get("pay_url")
            )
            if not invoice_id or not pay_url:
                logger.error(f"CryptoBot API не вернул invoice_id/url: {result}")
                raise RuntimeError("CryptoBot API не вернул данные инвойса")

            from bot.utils.key_generator import generate_qr_code
            qr_image_data = generate_qr_code(pay_url)

            logger.info(
                f"CryptoBot инвойс создан: id={invoice_id}, order_id={order_id}, "
                f"amount={amount_rub} RUB"
            )
            return {
                'provider_invoice_id': str(invoice_id),
                'pay_url': pay_url,
                'qr_image_data': qr_image_data,
                'status': result.get('status', 'active'),
            }


async def check_cryptobot_invoice_status(invoice_id: str) -> str:
    """
    Проверяет статус инвойса CryptoBot.

    GET https://pay.crypt.bot/api/getInvoices?invoice_ids={id}

    Returns:
        'pending' | 'succeeded' | 'canceled'
    """
    from database.requests import get_cryptobot_token
    token = get_cryptobot_token()
    if not token:
        raise ValueError("CryptoBot: API-токен не настроен")

    headers = {"Crypto-Pay-API-Token": token}
    params = {"invoice_ids": str(invoice_id)}

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{CRYPTOBOT_API_URL}/getInvoices", params=params, headers=headers
        ) as response:
            data = await response.json(content_type=None)

            if not data.get("ok"):
                error = data.get("error", {})
                desc = error.get("name") or str(error) or "Неизвестная ошибка"
                logger.error(f"CryptoBot статус ошибка: {desc}")
                raise RuntimeError(f"CryptoBot API ошибка: {desc}")

            items = data.get("result", {}).get("items", [])
            if not items:
                return 'pending'

            status = str(items[0].get("status", "")).lower()
            if status == 'paid':
                return 'succeeded'
            if status == 'expired':
                return 'canceled'
            return 'pending'


# ============================================================================
# HELEKET — крипто-эквайринг (https://doc.heleket.com), ex-Cryptomus API
# ============================================================================

HELEKET_API_URL = "https://api.heleket.com/v1"


def _heleket_sign(payload_json: str, api_key: str) -> str:
    """Подпись Heleket: MD5(base64(json_body) + api_key)."""
    encoded = base64.b64encode(payload_json.encode('utf-8')).decode('utf-8')
    return hashlib.md5((encoded + api_key).encode('utf-8')).hexdigest()


async def _heleket_request(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Выполняет подписанный POST-запрос к Heleket API."""
    import json as _json
    from database.requests import get_heleket_credentials
    merchant_id, api_key = get_heleket_credentials()
    if not merchant_id or not api_key:
        raise ValueError("Heleket: не настроены merchant_id или api_key")

    payload_json = _json.dumps(payload, separators=(',', ':'))
    headers = {
        "merchant": merchant_id,
        "sign": _heleket_sign(payload_json, api_key),
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HELEKET_API_URL}/{endpoint}", data=payload_json, headers=headers
        ) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                text = await response.text()
                logger.error(f"Heleket API: невозможно разобрать ответ ({response.status}): {text}")
                raise RuntimeError("Heleket API вернул некорректный ответ")

            if response.status not in (200, 201) or data.get('state') not in (0, None):
                desc = (
                    data.get('message') or data.get('error')
                    or str(data.get('errors', '')) or 'Неизвестная ошибка'
                )
                logger.error(f"Heleket API ошибка {response.status}: {desc} | payload={payload}")
                raise RuntimeError(f"Heleket API ошибка: {desc}")

            return data.get('result', data)


async def create_heleket_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    bot_name: str
) -> Dict[str, Any]:
    """
    Создаёт крипто-платёж в Heleket (сумма в RUB, оплата любой криптой).

    POST https://api.heleket.com/v1/payment

    Returns:
        Словарь с ключами provider_invoice_id, pay_url, qr_image_data, status
    """
    return_url = f"https://t.me/{bot_name}" if bot_name else "https://t.me"
    payload = {
        "amount": f"{float(amount_rub):.2f}",
        "currency": "RUB",
        "order_id": order_id,
        "url_return": return_url,
        "url_success": return_url,
        "lifetime": 3600,
    }

    result = await _heleket_request("payment", payload)

    invoice_uuid = result.get('uuid')
    pay_url = result.get('url')
    if not invoice_uuid or not pay_url:
        logger.error(f"Heleket API не вернул uuid/url: {result}")
        raise RuntimeError("Heleket API не вернул данные платежа")

    from bot.utils.key_generator import generate_qr_code
    qr_image_data = generate_qr_code(pay_url)

    logger.info(
        f"Heleket платёж создан: uuid={invoice_uuid}, order_id={order_id}, "
        f"amount={amount_rub} RUB"
    )
    return {
        'provider_invoice_id': str(invoice_uuid),
        'pay_url': pay_url,
        'qr_image_data': qr_image_data,
        'status': str(result.get('status', 'check')).lower(),
    }


async def check_heleket_payment_status(invoice_uuid: str) -> str:
    """
    Проверяет статус платежа Heleket.

    POST https://api.heleket.com/v1/payment/info

    Returns:
        'pending' | 'succeeded' | 'canceled'
    """
    result = await _heleket_request("payment/info", {"uuid": invoice_uuid})
    status = str(result.get('payment_status') or result.get('status') or '').lower()

    if status in ('paid', 'paid_over'):
        return 'succeeded'
    if status in ('cancel', 'fail', 'system_fail', 'refund_paid'):
        return 'canceled'
    return 'pending'


# ============================================================================
# ПРОМОКОДЫ
# ============================================================================

async def apply_promo_effect(user_id: int, promo: Dict[str, Any]) -> str:
    """
    Применяет эффект активированного промокода.

    Вызывается ПОСЛЕ успешной активации (db_promos.activate_promo).

    Args:
        user_id: Внутренний ID пользователя
        promo: dict промокода (promo_type, value)

    Returns:
        Текст для пользователя
    """
    from database.requests import (
        add_to_balance, add_days_to_first_active_key, set_user_next_discount
    )
    from database.connection import get_db as _get_db

    promo_type = promo['promo_type']
    value = promo['value'] or 0

    if promo_type == 'balance':
        from bot.services.user_locks import user_locks
        async with user_locks[user_id]:
            add_to_balance(user_id, value)
        return f"✅ Промокод активирован!\n\n💎 На баланс зачислено <b>{value / 100:g} ₽</b>."

    if promo_type == 'days':
        key_id = add_days_to_first_active_key(user_id, value)
        if key_id:
            try:
                from bot.services.vpn_api import push_key_to_panel
                await push_key_to_panel(key_id, reset_traffic=False)
            except Exception as e:
                logger.error(f"promo days: push_key_to_panel({key_id}) failed: {e}")
                from bot.services.alerts import send_admin_alert
                # bot may not be available here — log only; caller can alert
                return (
                    f"✅ Промокод активирован!\n\n📅 Ключ продлён на <b>{value} дн.</b> в базе, "
                    f"но синхронизация с панелью не удалась. Обратитесь в поддержку."
                )
            return f"✅ Промокод активирован!\n\n📅 Ваш ключ продлён на <b>{value} дн.</b>"
        return (
            "✅ Промокод принят, но у вас нет активного ключа для продления.\n"
            "Обратитесь в поддержку."
        )

    if promo_type == 'percent':
        set_user_next_discount(user_id, value)
        return (
            f"✅ Промокод активирован!\n\n"
            f"🏷️ Скидка <b>{value}%</b> будет применена к следующей покупке."
        )

    if promo_type == 'trial':
        with _get_db() as conn:
            conn.execute("UPDATE users SET used_trial = 0 WHERE id = ?", (user_id,))
        return "✅ Промокод активирован!\n\n🎁 Пробная подписка снова доступна."

    return "✅ Промокод активирован."


def get_discounted_tariff(user_id: int, tariff: Dict[str, Any]) -> Dict[str, Any]:
    """
    Возвращает копию тарифа с применённой персональной скидкой пользователя.

    Скидка (users.next_discount_percent) устанавливается промокодом типа
    'percent' и сжигается после успешной оплаты (consume_user_discount).

    Args:
        user_id: Внутренний ID пользователя
        tariff: dict тарифа (price_cents, price_stars, price_rub)

    Returns:
        dict тарифа с пересчитанными ценами + поле discount_percent
    """
    from database.requests import get_user_next_discount

    discount = get_user_next_discount(user_id)
    result = dict(tariff)
    result['discount_percent'] = discount
    if discount <= 0:
        return result

    factor = (100 - discount) / 100
    if result.get('price_cents'):
        result['price_cents'] = max(1, int(result['price_cents'] * factor))
    if result.get('price_stars'):
        result['price_stars'] = max(1, int(result['price_stars'] * factor))
    if result.get('price_rub'):
        result['price_rub'] = max(1, int(result['price_rub'] * factor))
    return result


def convert_to_rub_cents(amount_raw: int, payment_type: str, usd_rub_rate: int) -> int:
    """
    Конвертировать сырую сумму в копейки рублей.

    Args:
        amount_raw: сырая сумма (звёзды/центы USDT/копейки рублей)
        payment_type: тип платежа ('stars', 'crypto', 'cards', 'yookassa_qr', 'wata', 'platega')
        usd_rub_rate: курс USD/RUB в копейках

    Returns:
        Сумма в копейках рублей
    """
    if payment_type == 'stars':
        usd_cents = int(amount_raw * STAR_TO_USD * 100)
        return usd_cents * usd_rub_rate // 100
    elif payment_type == 'crypto':
        usd_cents = amount_raw
        return usd_cents * usd_rub_rate // 100
    else:
        return amount_raw


async def process_referral_reward(
    payer_id: int,
    period_days: int,
    amount_raw: int,
    payment_type: str
) -> None:
    """
    Обработка реферального вознаграждения при оплате.
    Вызывается ПОСЛЕ успешной обработки платежа.
    
    Args:
        payer_id: Внутренний ID пользователя, который оплатил
        period_days: Сколько дней купил реферал
        amount_raw: СЫРАЯ сумма:
            - 'stars': количество звёзд (int)
            - 'crypto': центы USDT (int)
            - 'cards': копейки рублей (int)
            - 'yookassa_qr': копейки рублей (int)
        payment_type: Тип платежа ('stars', 'crypto', 'cards', 'yookassa_qr')
    
    Note:
        При оплате балансом реферальные вознаграждения НЕ начисляются,
        поэтому эта функция не вызывается для платежей балансом.
    """
    if not is_referral_enabled():
        return
    
    reward_type = get_referral_reward_type()
    levels = get_active_referral_levels()
    
    if not levels:
        return
    
    usd_rub_rate = await get_usd_rub_rate()
    amount_rub_cents = convert_to_rub_cents(amount_raw, payment_type, usd_rub_rate)
    
    current_user_id = payer_id
    
    from bot.services.user_locks import user_locks
    
    for level_num, percent in levels:
        referrer_id = get_user_referrer(current_user_id)
        if not referrer_id:
            break
        
        coefficient = get_user_referral_coefficient(referrer_id)
        
        if reward_type == 'balance':
            base_reward = amount_rub_cents * (percent / 100)
            final_reward = int(base_reward * coefficient)
            final_reward = round(final_reward / 100) * 100
            
            if final_reward > 0:
                async with user_locks[referrer_id]:
                    add_to_balance(referrer_id, final_reward)
            
            reward_days = 0
        else:
            base_days = period_days * (percent / 100)
            final_days = base_days * coefficient
            reward_days = math.ceil(final_days)
            
            if reward_days > 0:
                key_id = add_days_to_first_active_key(referrer_id, reward_days)
                if key_id:
                    try:
                        from bot.services.vpn_api import push_key_to_panel
                        await push_key_to_panel(key_id, reset_traffic=False)
                    except Exception as e:
                        logger.error(
                            f"referral days: push_key_to_panel({key_id}) failed: {e}"
                        )
            
            final_reward = 0
        
        update_referral_stat(
            referrer_id, payer_id, level_num,
            final_reward, reward_days
        )
        
        current_user_id = referrer_id


def calculate_balance_discount(user_id: int, tariff_price_cents: int) -> tuple[int, int]:
    """
    Рассчитать скидку с баланса. БЕЗ списания!
    
    Args:
        user_id: Внутренний ID пользователя
        tariff_price_cents: Цена тарифа в копейках
    
    Returns:
        Кортеж (remaining_to_pay_cents, to_deduct_cents):
        - remaining_to_pay_cents: сколько нужно оплатить внешним способом
        - to_deduct_cents: сколько будет списано с баланса ПРИ УСПЕШНОЙ оплате
    """
    balance = get_user_balance(user_id)
    
    if balance >= tariff_price_cents:
        return 0, tariff_price_cents
    else:
        return tariff_price_cents - balance, balance


async def complete_payment_flow(
    order_id: str,
    message,
    state,
    telegram_id: int,
    payment_type: str,
    referral_amount: int = 0,
) -> None:
    """
    Единый post-payment поток после подтверждения оплаты (check-кнопка / Stars / Cards).

    fulfill → referral once → finalize_payment_ui (с config FSM для draft).
    referral_amount оставлен для совместимости; фактическая сумма берётся из ордера.
    """
    from bot.handlers.user.payments.base import finalize_payment_ui
    from bot.keyboards.admin import home_only_kb

    try:
        (success, text, order) = await fulfill_paid_order(order_id)

        if success and order:
            await state.update_data(balance_to_deduct=0, remaining_cents=0)
            await pay_referral_once(order)
            await finalize_payment_ui(message, state, text, order, user_id=telegram_id)
        else:
            await message.answer(text, reply_markup=home_only_kb(), parse_mode='HTML')

    except Exception as e:
        from bot.errors import TariffNotFoundError
        if isinstance(e, TariffNotFoundError):
            from bot.keyboards.user import support_kb
            support_link = get_setting('support_channel_link', '') or ''
            await message.answer(str(e), reply_markup=support_kb(support_link), parse_mode='HTML')
        else:
            logger.exception(f'Ошибка обработки {payment_type} платежа: {e}')
            await message.answer('❌ Произошла ошибка при обработке платежа.', parse_mode='HTML')

