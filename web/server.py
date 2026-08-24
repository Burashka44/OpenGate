"""
Встроенный aiohttp веб-сервер OpenGate.

Работает внутри процесса бота (единый event loop, единая SQLite-БД в WAL).
Включается настройкой web_enabled=1; порт — web_port (по умолчанию 8081).
Наружу публикуется через reverse-proxy (nginx/caddy) по адресу web_public_url.

Маршруты:
- GET  /healthz                — liveness-проба
- GET  /sub/{token}            — брендированная subscription page ключа
- POST /webhook/cryptobot      — вебхук Crypto Pay (мгновенное зачисление)
- POST /webhook/heleket        — вебхук Heleket
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)

BOT_APP_KEY = web.AppKey("bot", object)


# ============================================================================
# SUBSCRIPTION PAGE
# ============================================================================

_SUB_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
  :root {{
    --bg: #0f1220; --card: #191d33; --text: #eef0ff; --muted: #9aa0c3;
    --accent: #6c7bff; --ok: #3ddc84; --warn: #ffb454; --bad: #ff5470;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; display: flex; justify-content: center; padding: 24px 16px;
  }}
  .wrap {{ width: 100%; max-width: 480px; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .sub {{ color: var(--muted); font-size: 14px; margin-bottom: 20px; }}
  .card {{
    background: var(--card); border-radius: 16px; padding: 20px; margin-bottom: 16px;
  }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0; font-size: 15px; }}
  .row .k {{ color: var(--muted); }}
  .status {{ font-weight: 600; }}
  .status.ok {{ color: var(--ok); }} .status.bad {{ color: var(--bad); }}
  .bar {{ height: 8px; background: #262b4a; border-radius: 4px; overflow: hidden; margin-top: 8px; }}
  .bar i {{ display: block; height: 100%; background: var(--accent); width: {traffic_pct}%; }}
  .link-box {{
    background: #10132a; border: 1px dashed #3a4066; border-radius: 10px;
    padding: 12px; font-family: monospace; font-size: 12px; word-break: break-all;
    color: var(--muted); margin: 12px 0;
  }}
  .btn {{
    display: block; text-align: center; text-decoration: none; color: #fff;
    background: var(--accent); border-radius: 12px; padding: 14px; font-size: 16px;
    font-weight: 600; margin-bottom: 10px; border: none; width: 100%; cursor: pointer;
  }}
  .btn.ghost {{ background: #262b4a; }}
  .apps {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .apps .btn {{ margin-bottom: 0; font-size: 14px; padding: 12px; }}
  .qr {{ text-align: center; margin: 16px 0; }}
  .qr img {{ width: 200px; height: 200px; border-radius: 12px; background: #fff; padding: 8px; }}
  .footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>🔐 {title}</h1>
  <div class="sub">Подписка {key_name}</div>

  <div class="card">
    <div class="row"><span class="k">Статус</span><span class="status {status_class}">{status_text}</span></div>
    <div class="row"><span class="k">Действует до</span><span>{expires_at}</span></div>
    <div class="row"><span class="k">Осталось</span><span>{days_left}</span></div>
    {traffic_block}
  </div>

  {sub_block}

  <div class="footer">Обновлено {updated_at} · OpenGate</div>
</div>
<script>
function copySub() {{
  var el = document.getElementById('suburl');
  if (!el) return;
  navigator.clipboard.writeText(el.textContent.trim()).then(function() {{
    var b = document.getElementById('copybtn');
    b.textContent = '✅ Скопировано';
    setTimeout(function() {{ b.textContent = '📋 Скопировать ссылку'; }}, 1500);
  }});
}}
</script>
</body>
</html>"""


def _build_sub_block(sub_url: Optional[str]) -> str:
    """HTML-блок со ссылкой подписки, QR и кнопками клиентов."""
    if not sub_url:
        return (
            '<div class="card">Ссылка подписки недоступна. '
            'Откройте бота и получите ключ в разделе «Мои ключи».</div>'
        )
    import urllib.parse
    encoded = urllib.parse.quote(sub_url, safe='')
    b64 = base64.b64encode(sub_url.encode()).decode()
    qr_src = f"/qr?data={encoded}"
    return f"""
  <div class="card">
    <div class="qr"><img src="{qr_src}" alt="QR"></div>
    <div class="link-box" id="suburl">{sub_url}</div>
    <button class="btn ghost" id="copybtn" onclick="copySub()">📋 Скопировать ссылку</button>
    <div class="apps">
      <a class="btn" href="happ://add/{sub_url}">Happ</a>
      <a class="btn" href="v2raytun://import/{sub_url}">v2RayTun</a>
      <a class="btn" href="streisand://import/{sub_url}">Streisand</a>
      <a class="btn" href="hiddify://import/{sub_url}">Hiddify</a>
      <a class="btn" href="v2box://install-sub?url={encoded}">V2Box</a>
      <a class="btn" href="sub://{b64}">FoXray</a>
    </div>
  </div>"""


async def handle_sub_page(request: web.Request) -> web.Response:
    """GET /sub/{token} — subscription page ключа."""
    from database.requests import get_key_by_page_token
    from bot.services.vpn_api import get_subscription_url_for_key, format_traffic
    from database.db_keys import is_key_active

    token = request.match_info.get('token', '')
    key = get_key_by_page_token(token)
    if not key:
        return web.Response(status=404, text="Not found")

    active = is_key_active(key)

    # Срок действия
    days_left = "—"
    expires_str = "—"
    try:
        expires = datetime.fromisoformat(str(key['expires_at']).replace('Z', ''))
        expires_str = expires.strftime("%d.%m.%Y %H:%M")
        delta = expires - datetime.now()
        days_left = f"{max(0, delta.days)} дн." if delta.total_seconds() > 0 else "истёк"
    except Exception:
        pass

    # Трафик
    traffic_block = ""
    traffic_pct = 0
    limit = key.get('traffic_limit', 0) or 0
    used = key.get('traffic_used', 0) or 0
    if limit > 0:
        traffic_pct = min(100, int(used / limit * 100))
        traffic_block = (
            f'<div class="row"><span class="k">Трафик</span>'
            f'<span>{format_traffic(used)} / {format_traffic(limit)}</span></div>'
            f'<div class="bar"><i></i></div>'
        )

    sub_url = await get_subscription_url_for_key(key)

    key_name = key.get('custom_name') or f"#{key['id']}"
    html = _SUB_PAGE_TEMPLATE.format(
        title=key.get('server_name') or "VPN",
        key_name=key_name,
        status_class="ok" if active else "bad",
        status_text="Активна" if active else "Неактивна",
        expires_at=expires_str,
        days_left=days_left,
        traffic_block=traffic_block,
        traffic_pct=traffic_pct,
        sub_block=_build_sub_block(sub_url if active else None),
        updated_at=datetime.now().strftime("%d.%m.%Y %H:%M"),
    )
    return web.Response(text=html, content_type='text/html')


async def handle_qr(request: web.Request) -> web.Response:
    """GET /qr?data=... — PNG QR-код (для subscription page)."""
    data = request.query.get('data', '')
    if not data or len(data) > 2048:
        return web.Response(status=400, text="Bad request")
    from bot.utils.key_generator import generate_qr_code
    png = generate_qr_code(data)
    return web.Response(body=png, content_type='image/png')


# ============================================================================
# ВЕБХУКИ ПЛАТЕЖЕЙ
# ============================================================================

async def _fulfill_webhook_order(request: web.Request, order_id: str, provider: str) -> None:
    """
    Общая выдача по вебхуку.

    При webhook_postpay_v2=1: fulfill → referral once → notify once (без FSM config).
    Иначе (legacy): process_payment_order + простое уведомление.
    """
    from database.requests import get_setting, get_user_by_id
    from bot.services.http_utils import log_ctx

    use_v2 = get_setting('webhook_postpay_v2', '0') == '1'
    bot = request.app.get(BOT_APP_KEY)
    log_ctx(logger, logging.INFO, f"Webhook {provider} fulfill start", order_id=order_id)

    if use_v2:
        from bot.services.billing import fulfill_paid_order, pay_referral_once, notify_payment_once
        success, text, order = await fulfill_paid_order(order_id)
        if not (success and order):
            logger.warning(f"Webhook {provider}: ордер {order_id} не обработан: {text}")
            return
        await pay_referral_once(order)
        if bot:
            await notify_payment_once(bot, order, text)
        try:
            from bot.services.alerts import send_payment_alert
            if bot:
                await send_payment_alert(bot, order)
        except Exception as e:
            logger.debug(f"Webhook {provider}: алерт не отправлен: {e}")
        return

    from bot.services.billing import process_payment_order
    success, text, order = await process_payment_order(order_id)
    if not (success and order):
        logger.warning(f"Webhook {provider}: ордер {order_id} не обработан: {text}")
        return

    if not bot:
        return
    user = get_user_by_id(order['user_id'])
    if not user:
        return
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")],
            [InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
        ])
        await bot.send_message(
            chat_id=user['telegram_id'], text=text, reply_markup=kb, parse_mode='HTML'
        )
    except Exception as e:
        logger.warning(f"Webhook {provider}: не удалось уведомить пользователя: {e}")

    try:
        from bot.services.alerts import send_payment_alert
        await send_payment_alert(bot, order)
    except Exception as e:
        logger.debug(f"Webhook {provider}: алерт не отправлен: {e}")


async def _enqueue_or_fulfill(
    request: web.Request, provider: str, event_id: str, order_id: str, payload: dict
) -> None:
    from database.requests import get_setting
    if get_setting('webhook_outbox_enabled', '1') == '1' and event_id:
        from bot.services.webhook_outbox import enqueue_webhook_event, process_outbox
        enqueued = enqueue_webhook_event(provider, event_id, order_id, payload)
        if enqueued:
            bot = request.app.get(BOT_APP_KEY)
            await process_outbox(bot)
        return
    await _fulfill_webhook_order(request, order_id, provider)


def _heleket_sign_ok(raw_body: bytes, api_key: str, data: dict) -> bool:
    """
    Проверка подписи Heleket с PHP slash-escape fallback и length-safe compare.
    """
    sign = data.get('sign') or ''
    if not isinstance(sign, str) or not sign:
        return False

    payload = {k: v for k, v in data.items() if k != 'sign'}

    def _md5_sign(obj: dict) -> str:
        payload_json = json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
        encoded = base64.b64encode(payload_json.encode()).decode()
        return hashlib.md5((encoded + api_key).encode()).hexdigest()

    candidates = [_md5_sign(payload)]
    # PHP json_encode escapes / as \/
    try:
        payload_json_slash = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).replace('/', '\\/')
        encoded = base64.b64encode(payload_json_slash.encode()).decode()
        candidates.append(hashlib.md5((encoded + api_key).encode()).hexdigest())
    except Exception:
        pass

    # Raw body without sign field (если пришло как есть)
    try:
        raw_obj = json.loads(raw_body)
        if isinstance(raw_obj, dict):
            raw_obj.pop('sign', None)
            candidates.append(_md5_sign(raw_obj))
    except Exception:
        pass

    for expected in candidates:
        if len(expected) != len(sign):
            continue
        if hmac.compare_digest(expected, sign):
            return True
    return False


async def handle_cryptobot_webhook(request: web.Request) -> web.Response:
    """
    POST /webhook/cryptobot — вебхук Crypto Pay API.

    Подпись: заголовок crypto-pay-api-signature =
    HMAC-SHA256(body, SHA256(app_token)).
    """
    from database.requests import get_cryptobot_token

    token = get_cryptobot_token()
    if not token:
        return web.Response(status=404, text="disabled")

    body = await request.read()
    signature = request.headers.get('crypto-pay-api-signature', '')
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or len(expected) != len(signature) \
            or not hmac.compare_digest(expected, signature):
        logger.warning("CryptoBot webhook: неверная подпись")
        return web.Response(status=403, text="bad signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")

    if data.get('update_type') == 'invoice_paid':
        payload = data.get('payload', {})
        order_id = payload.get('payload', '')
        invoice_id = str(payload.get('invoice_id') or payload.get('id') or order_id)
        if order_id:
            asyncio.create_task(
                _enqueue_or_fulfill(request, 'cryptobot', f"cb:{invoice_id}", order_id, data)
            )

    return web.Response(text="ok")


async def handle_heleket_webhook(request: web.Request) -> web.Response:
    """
    POST /webhook/heleket — вебхук Heleket.

    Подпись: поле sign = MD5(base64(json без sign) + api_key).
    """
    from database.requests import get_heleket_credentials

    _, api_key = get_heleket_credentials()
    if not api_key:
        return web.Response(status=404, text="disabled")

    raw_body = await request.read()
    try:
        data = json.loads(raw_body)
    except Exception:
        return web.Response(status=400, text="bad json")

    if not isinstance(data, dict) or not _heleket_sign_ok(raw_body, api_key, data):
        logger.warning("Heleket webhook: неверная подпись")
        return web.Response(status=403, text="bad signature")

    status = str(data.get('status', '')).lower()
    order_id = data.get('order_id', '')
    event_id = str(data.get('uuid') or data.get('txid') or order_id)
    if status in ('paid', 'paid_over') and order_id:
        asyncio.create_task(
            _enqueue_or_fulfill(request, 'heleket', f"hk:{event_id}", order_id, data)
        )

    return web.Response(text="ok")


async def handle_healthz(request: web.Request) -> web.Response:
    """GET /healthz — liveness-проба."""
    return web.json_response({"status": "ok"})


# ============================================================================
# ЗАПУСК/ОСТАНОВКА
# ============================================================================

def create_app(bot) -> web.Application:
    """Создаёт aiohttp-приложение с маршрутами."""
    app = web.Application()
    app[BOT_APP_KEY] = bot
    app.router.add_get('/healthz', handle_healthz)
    app.router.add_get('/sub/{token}', handle_sub_page)
    app.router.add_get('/qr', handle_qr)
    app.router.add_post('/webhook/cryptobot', handle_cryptobot_webhook)
    app.router.add_post('/webhook/heleket', handle_heleket_webhook)
    return app


async def start_web_server(bot) -> Optional[web.AppRunner]:
    """
    Запускает веб-сервер, если web_enabled=1.

    Returns:
        AppRunner для последующей остановки или None
    """
    from database.requests import is_web_enabled, get_web_port

    if not is_web_enabled():
        logger.info("🌐 Веб-сервер отключён (web_enabled=0)")
        return None

    port = get_web_port()
    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=port)
    await site.start()
    logger.info(f"🌐 Веб-сервер запущен на 0.0.0.0:{port}")
    return runner


async def stop_web_server(runner: Optional[web.AppRunner]) -> None:
    """Останавливает веб-сервер."""
    if runner:
        await runner.cleanup()
        logger.info("🌐 Веб-сервер остановлен")
