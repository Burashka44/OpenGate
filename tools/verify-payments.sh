#!/bin/bash
# Проверка платёжного стека после очистки импортов (запускать на VPS в /opt/OpenGate)
set -e

echo "=== 1. Сервис ==="
systemctl is-active opengate && echo "opengate: active" || { echo "opengate: NOT active"; exit 1; }
journalctl -u opengate -n 30 --no-pager | grep -iE 'ImportError|ModuleNotFoundError|Traceback' && exit 1 || echo "journal: no import errors in last 30 lines"

echo ""
echo "=== 2. Версия кода ==="
git log -1 --oneline
grep -n "pre_checkout_handler" bot/handlers/user/payments/base.py | head -1
! grep -q "PreCheckoutQuery" bot/handlers/user/payments/stars.py && echo "stars.py: no PreCheckoutQuery (OK)"
! grep -q "PreCheckoutQuery" bot/handlers/user/payments/crypto.py && echo "crypto.py: no PreCheckoutQuery (OK)"

echo ""
echo "=== 3. Настройки в БД (включённые методы) ==="
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("database/vpn_bot.db")
keys = [
    "stars_enabled", "crypto_enabled", "cards_enabled",
    "yookassa_qr_enabled", "wata_enabled", "platega_enabled",
    "demo_payment_enabled",
]
for k in keys:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    v = row[0] if row else "(нет)"
    print(f"  {k} = {v}")
conn.close()
PY

echo ""
echo "=== 4. Импорт модулей Python ==="
./venv/bin/python -c "
from bot.handlers.user.payments import router
from bot.handlers.user.payments.base import pre_checkout_handler
from bot.handlers.user.payments.stars import router as s
from bot.handlers.user.payments.crypto import router as c
from bot.handlers.user.payments.balance import router as b
from bot.services.billing import complete_payment_flow
print('import chain: OK')
"

echo ""
echo "=== 5. Лог Stars (после тестовой оплаты вручную) ==="
echo "  journalctl -u opengate -f"
echo "  Ожидайте: Успешная оплата stars:"

echo ""
echo "=== Готово. Smoke-тест Stars/крипто/агрегаторов — в Telegram вручную. ==="
