#!/usr/bin/env python3
"""
Проверка платёжного стека после очистки импортов.
Запуск: из корня OpenGate — python tools/verify_payments.py
На VPS: ./venv/bin/python tools/verify_payments.py
"""
from __future__ import annotations

import ast
import compileall
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAYMENTS = ROOT / "bot" / "handlers" / "user" / "payments"
DB_PATH = ROOT / "database" / "vpn_bot.db"

# Символы, которые не должны быть в stars/crypto/balance (только в base.py)
FORBIDDEN_IN = {
    "stars.py": {"PreCheckoutQuery", "Message", "Command", "ADMIN_IDS"},
    "crypto.py": {"PreCheckoutQuery", "Message", "Command", "ADMIN_IDS", "LabeledPrice"},
    "balance.py": {"PreCheckoutQuery", "Message", "Command", "ADMIN_IDS"},
}

REQUIRED_IN = {
    "base.py": {"pre_checkout_handler", "successful_payment_handler", "PreCheckoutQuery"},
}

SETTINGS_KEYS = [
    "stars_enabled",
    "crypto_enabled",
    "cards_enabled",
    "yookassa_qr_enabled",
    "wata_enabled",
    "platega_enabled",
    "demo_payment_enabled",
]

SUCCESS_LOG = "Успешная оплата"


def check_compile() -> list[str]:
    errors = []
    ok = compileall.compile_dir(str(PAYMENTS), quiet=1)
    if not ok:
        errors.append("py_compile: ошибки в bot/handlers/user/payments/")
    return errors


def check_static_imports() -> list[str]:
    errors = []
    for fname, forbidden in FORBIDDEN_IN.items():
        text = (PAYMENTS / fname).read_text(encoding="utf-8")
        for sym in forbidden:
            if sym in text.split("def ", 1)[0]:  # только блок импортов + до первой def
                # уточнение: ищем в первых 40 строках
                head = "\n".join(text.splitlines()[:40])
                if sym in head:
                    errors.append(f"{fname}: лишний символ {sym} в начале файла")
    base = (PAYMENTS / "base.py").read_text(encoding="utf-8")
    for sym in REQUIRED_IN["base.py"]:
        if sym not in base:
            errors.append(f"base.py: отсутствует {sym}")
    if SUCCESS_LOG not in base:
        errors.append("base.py: нет строки лога успешной оплаты")
    return errors


def check_router_order() -> list[str]:
    errors = []
    init = (PAYMENTS / "__init__.py").read_text(encoding="utf-8")
    if "base_router" not in init or init.find("base_router") > init.find("stars_router"):
        # base должен подключаться раньше stars
        lines = init.splitlines()
        base_i = stars_i = -1
        for i, line in enumerate(lines):
            if "base_router" in line:
                base_i = i
            if "stars_router" in line or "from .stars" in line:
                stars_i = i if stars_i < 0 else stars_i
        if base_i < 0:
            errors.append("__init__.py: base_router не найден")
        elif stars_i >= 0 and base_i > stars_i:
            errors.append("__init__.py: base_router должен быть раньше stars")
    return errors


def check_import_chain() -> list[str]:
    errors = []
    sys.path.insert(0, str(ROOT))
    try:
        import aiogram  # noqa: F401
    except ModuleNotFoundError:
        return [
            "import chain: SKIP (install deps: pip install -r requirements.txt)"
        ]
    try:
        from bot.handlers.user.payments import router  # noqa: F401
        from bot.handlers.user.payments.base import (
            pre_checkout_handler,
            successful_payment_handler,
        )
        from bot.handlers.user.payments.stars import router as stars_router  # noqa: F401
        from bot.handlers.user.payments.crypto import router as crypto_router  # noqa: F401
        from bot.handlers.user.payments.balance import router as balance_router  # noqa: F401
        from bot.services.billing import complete_payment_flow  # noqa: F401
    except Exception as e:
        errors.append(f"import chain: {type(e).__name__}: {e}")
    return errors


def check_settings_db() -> tuple[list[str], list[str]]:
    """Возвращает (errors, info_lines)."""
    errors = []
    info = []
    if not DB_PATH.is_file():
        info.append(f"БД не найдена ({DB_PATH}) — на VPS проверьте админку вручную")
        return errors, info
    conn = sqlite3.connect(DB_PATH)
    try:
        for key in SETTINGS_KEYS:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            val = row[0] if row else "(нет)"
            info.append(f"  {key} = {val}")
        # ключи без публикации значений
        for pattern in ("yookassa_%", "crypto_%", "wata_%", "platega_%"):
            rows = conn.execute(
                "SELECT key FROM settings WHERE key LIKE ? AND value != '' AND value IS NOT NULL",
                (pattern,),
            ).fetchall()
            if rows:
                info.append(f"  заполнено ключей {pattern}: {len(rows)}")
    except sqlite3.Error as e:
        errors.append(f"БД settings: {e}")
    finally:
        conn.close()
    return errors, info


def main() -> int:
    print("=== verify_payments (после очистки импортов) ===\n")
    all_errors: list[str] = []

    print("1. py_compile payments/")
    all_errors.extend(check_compile())
    print("   OK" if not all_errors else "   FAIL")

    print("2. Статика: импорты stars/crypto/balance, base.py")
    static_err = check_static_imports()
    all_errors.extend(static_err)
    print("   OK" if not static_err else "\n   ".join(["   FAIL"] + static_err))

    print("3. Порядок роутеров (__init__.py)")
    order_err = check_router_order()
    all_errors.extend(order_err)
    print("   OK" if not order_err else "   FAIL")

    print("4. Import chain (aiogram + handlers)")
    imp_err = check_import_chain()
    skip_imp = imp_err and imp_err[0].startswith("import chain: SKIP")
    if skip_imp:
        print(f"   {imp_err[0]}")
    elif imp_err:
        all_errors.extend(imp_err)
        print(f"   FAIL: {imp_err[0]}")
    else:
        print("   OK")

    print("5. Настройки в БД")
    db_err, db_info = check_settings_db()
    all_errors.extend(db_err)
    for line in db_info:
        print(line)
    if db_err:
        print("   FAIL")
    elif db_info and "не найдена" in db_info[0]:
        print("   SKIP (локально — на проде выполните с vpn_bot.db)")
    else:
        print("   OK")

    print("\n6. Smoke-test (Telegram, manually)")
    print("   Stars: buy key -> Stars -> tariff -> XTR -> key issued")
    print("   Crypto/balance/aggregators: only methods enabled in admin")
    print("\n7. VPS log after Stars payment:")
    print("   journalctl -u opengate -n 100 | grep successful payment log line")

    if all_errors:
        print("\nFAILED:", len(all_errors), "ошибок")
        for e in all_errors:
            print(" -", e)
        return 1
    print("\nPASSED: автоматические проверки пройдены.")
    print("Осталось: systemctl на VPS + smoke в Telegram (см. tools/verify-payments.sh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
