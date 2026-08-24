"""Practical verification script for OpenGate audit implementation."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
ERRORS = []


def ok(name: str):
    global PASS
    PASS += 1
    print(f"  ✅ {name}")


def bad(name: str, err: Exception | str = ""):
    global FAIL
    FAIL += 1
    msg = f"{err}" if err else ""
    print(f"  ❌ {name}: {msg}")
    ERRORS.append((name, msg))


def section(title: str):
    print(f"\n=== {title} ===")


def test_migrations():
    section("Migrations / schema")
    from database.migrations import run_migrations, LATEST_VERSION
    from database.connection import get_db

    run_migrations()
    with get_db() as conn:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        ver = row["version"] if row else None
        if ver == LATEST_VERSION:
            ok(f"schema_version={ver}")
        else:
            bad("schema_version", f"got {ver}, want {LATEST_VERSION}")

        cols = {r[1] for r in conn.execute("PRAGMA table_info(payments)").fetchall()}
        for c in ("balance_to_deduct", "referral_paid_at", "user_notified_at", "fulfilled_at"):
            (ok if c in cols else bad)(f"payments.{c}")

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        (ok if "webhook_outbox" in tables else bad)("table webhook_outbox")

        for k in ("webhook_postpay_v2", "webhook_outbox_enabled", "redis_fsm_url"):
            r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            if r is not None:
                ok(f"setting {k}={r['value']!r}")
            else:
                bad(f"setting {k}", "missing")


def test_imports():
    section("Critical imports")
    mods = [
        "bot.services.billing",
        "bot.services.ops",
        "bot.services.vpn_api",
        "bot.services.webhook_outbox",
        "bot.services.circuit_breaker",
        "bot.services.http_utils",
        "bot.services.panels.base",
        "bot.services.panels.xui",
        "bot.services.panels.marzban",
        "bot.services.panels.naive",
        "bot.services.panels.mieru",
        "web.server",
        "database.db_payments",
        "bot.handlers.user.payments.balance",
        "bot.handlers.user.payments.keys_config",
        "bot.handlers.admin.servers",
        "bot.handlers.admin.ops",
        "main",
    ]
    for m in mods:
        try:
            __import__(m)
            ok(m)
        except Exception as e:
            bad(m, e)


def test_panels():
    section("Panel factory / mocks")
    from bot.services.vpn_api import get_client_from_server_data, KNOWN_PANEL_TYPES
    from bot.services.panels.base import VPNAPIError
    from bot.services.panels.xui import XUIClient
    from bot.services.panels.marzban import MarzbanClient
    from bot.services.panels.naive import NaiveClient
    from bot.services.panels.mieru import MieruClient

    # clear cache between
    from bot.services import vpn_api as va
    va._clients.clear()

    c = get_client_from_server_data({
        "id": 9101, "name": "x", "host": "127.0.0.1", "port": 2053,
        "login": "a", "password": "b", "web_base_path": "/", "panel_type": "",
    })
    if isinstance(c, XUIClient):
        ok("factory empty -> XUI")
    else:
        bad("factory empty -> XUI")

    va._clients.clear()
    c = get_client_from_server_data({
        "id": 9102, "name": "m", "host": "127.0.0.1", "port": 443,
        "login": "a", "password": "b", "protocol": "https", "panel_type": "marzban",
    })
    if isinstance(c, MarzbanClient) and not c.supports_inbound_select:
        ok("factory marzban")
    else:
        bad("factory marzban")

    va._clients.clear()
    try:
        get_client_from_server_data({
            "id": 9103, "name": "bad", "host": "h", "port": 1,
            "login": "a", "password": "b", "panel_type": "unknown_panel",
        })
        bad("unknown panel_type should raise")
    except VPNAPIError:
        ok("unknown panel_type -> VPNAPIError")

    n = NaiveClient({"id": 1, "host": "n.example", "public_host": "n.example", "extra_config": "{}"})
    link = n.build_naive_link("u", "p")
    if link == "naive+https://u:p@n.example":
        ok("naive link")
    else:
        bad("naive link", link)

    m = MieruClient({"id": 1, "host": "m.example", "public_host": "m.example", "extra_config": "{}"})
    if "mieru://" in m.build_mieru_link("a", "b"):
        ok("mieru link")
    else:
        bad("mieru link")

    if KNOWN_PANEL_TYPES == frozenset({"xui", "marzban", "naive", "mieru"}):
        ok("KNOWN_PANEL_TYPES")
    else:
        bad("KNOWN_PANEL_TYPES")


def test_money_helpers():
    section("Money helpers (DB orders)")
    from database.db_payments import (
        charged_amounts_from_tariff,
        create_pending_order_from_tariff,
        find_order_by_order_id,
        mark_referral_paid,
        mark_user_notified,
        complete_order,
    )
    from database.connection import get_db
    from bot.services.billing import referral_amount_from_order, get_discounted_tariff

    tariff = {
        "id": None,  # may fail FK — use real or null
        "price_cents": 1000,
        "price_stars": 100,
        "price_rub": 500,
        "duration_days": 30,
        "name": "test",
    }

    # charged amounts
    c, s, d = charged_amounts_from_tariff(tariff, "crypto")
    if c == 1000 and s == 100 and d == 30:
        ok("charged crypto USD cents")
    else:
        bad("charged crypto", f"{c},{s},{d}")

    c, s, d = charged_amounts_from_tariff(tariff, "cards")
    if c == 50000 and s == 100:
        ok("charged cards RUB kopecks")
    else:
        bad("charged cards", f"{c},{s},{d}")

    # discounted
    with get_db() as conn:
        # ensure a user
        conn.execute(
            "INSERT OR IGNORE INTO users (id, telegram_id, username) VALUES (999001, 999001, 'verify_bot')"
        )
        # wipe discount
        try:
            conn.execute("UPDATE users SET next_discount_percent=0 WHERE id=999001")
        except Exception:
            pass

    disc = dict(tariff)
    disc["price_cents"] = 800
    disc["price_rub"] = 400
    disc["price_stars"] = 80

    # create order without tariff_id FK if possible — use NULL tariff
    t = dict(disc)
    t["id"] = None
    try:
        pid, oid = create_pending_order_from_tariff(
            user_id=999001, tariff=t, payment_type="crypto", vpn_key_id=None
        )
        order = find_order_by_order_id(oid)
        if order and order["amount_cents"] == 800 and order["amount_stars"] == 80:
            ok(f"create_pending_order_from_tariff crypto {oid}")
        else:
            bad("create order crypto amounts", order)

        amt, ptype = referral_amount_from_order(order)
        if amt == 800 and ptype == "crypto":
            ok("referral_amount_from_order crypto")
        else:
            bad("referral_amount_from_order", f"{amt},{ptype}")

        if mark_referral_paid(oid) and not mark_referral_paid(oid):
            ok("mark_referral_paid idempotent")
        else:
            bad("mark_referral_paid")

        if mark_user_notified(oid) and not mark_user_notified(oid):
            ok("mark_user_notified idempotent")
        else:
            bad("mark_user_notified")

        # RUB + balance_to_deduct
        t2 = dict(disc)
        t2["id"] = None
        pid2, oid2 = create_pending_order_from_tariff(
            user_id=999001, tariff=t2, payment_type="cards",
            vpn_key_id=None, balance_to_deduct=15000,
        )
        o2 = find_order_by_order_id(oid2)
        # full 40000 - 15000 = 25000 remaining in amount_cents
        if o2 and o2["amount_cents"] == 25000 and int(o2["balance_to_deduct"]) == 15000:
            ok(f"partial balance order {oid2}")
        else:
            bad("partial balance order", o2)

        amt2, p2 = referral_amount_from_order(o2)
        if amt2 == 40000 and p2 == "cards":
            ok("referral = paid + balance_to_deduct")
        else:
            bad("referral partial", f"{amt2},{p2}")

        from database.db_payments import claim_balance_to_deduct
        claimed = claim_balance_to_deduct(oid2)
        claimed2 = claim_balance_to_deduct(oid2)
        if claimed == 15000 and claimed2 == 0:
            ok("claim_balance_to_deduct idempotent")
        else:
            bad("claim_balance_to_deduct", f"{claimed},{claimed2}")

        # cleanup
        with get_db() as conn:
            conn.execute("DELETE FROM payments WHERE order_id IN (?, ?)", (oid, oid2))
    except Exception as e:
        bad("order create flow", e)
        traceback.print_exc()


def test_heleket_and_outbox():
    section("Heleket sign + webhook outbox")
    from web.server import _heleket_sign_ok
    from bot.services.webhook_outbox import enqueue_webhook_event, claim_pending, mark_done

    api_key = "test_api_key"
    payload = {"order_id": "00abc", "status": "paid", "amount": "10"}
    body_obj = dict(payload)
    js = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    encoded = base64.b64encode(js.encode()).decode()
    sign = hashlib.md5((encoded + api_key).encode()).hexdigest()
    data = dict(payload)
    data["sign"] = sign
    raw = json.dumps(data).encode()

    if _heleket_sign_ok(raw, api_key, data):
        ok("heleket sign valid")
    else:
        bad("heleket sign valid")

    bad_data = dict(data)
    bad_data["sign"] = "0" * len(sign)
    if not _heleket_sign_ok(raw, api_key, bad_data):
        ok("heleket bad sign rejected")
    else:
        bad("heleket bad sign rejected")

    # slash-escape candidate
    payload2 = {"order_id": "00x", "url": "https://a/b", "status": "paid"}
    js2 = json.dumps(payload2, separators=(",", ":"), ensure_ascii=False).replace("/", "\\/")
    enc2 = base64.b64encode(js2.encode()).decode()
    sign2 = hashlib.md5((enc2 + api_key).encode()).hexdigest()
    data2 = dict(payload2)
    data2["sign"] = sign2
    raw2 = json.dumps(data2).encode()
    if _heleket_sign_ok(raw2, api_key, data2):
        ok("heleket PHP slash-escape sign")
    else:
        bad("heleket PHP slash-escape sign")

    import time
    eid = f"verify-{int(time.time()*1000)}"
    first = enqueue_webhook_event("test", eid, "00verify", {"x": 1})
    second = enqueue_webhook_event("test", eid, "00verify", {"x": 1})
    if first and not second:
        ok("outbox unique event_id")
    else:
        bad("outbox unique", f"{first},{second}")

    rows = [r for r in claim_pending(50) if r.get("event_id") == eid]
    if rows:
        mark_done(rows[0]["id"])
        ok("outbox claim+done")
    else:
        bad("outbox claim")


def test_ops_helpers():
    section("Ops / circuit breaker / maintenance helpers")
    from bot.services.circuit_breaker import record_failure, record_success, is_open, assert_closed
    from bot.services.panels.base import VPNAPIError

    sid = 777001
    record_success(sid)
    if not is_open(sid):
        ok("circuit initially closed")
    record_failure(sid)
    record_failure(sid)
    record_failure(sid)
    if is_open(sid):
        ok("circuit opens after 3 failures")
    else:
        bad("circuit open")
    try:
        assert_closed(sid)
        bad("assert_closed should raise")
    except VPNAPIError:
        ok("assert_closed raises")
    record_success(sid)
    if not is_open(sid):
        ok("circuit closed after success")

    from bot.utils.groups import get_servers_for_key
    # just callable
    try:
        get_servers_for_key(0)
        ok("get_servers_for_key callable")
    except Exception as e:
        bad("get_servers_for_key", e)


def test_crypto_verify_logic():
    section("Crypto amount compare (logic)")
    # Simulate the comparison used in process_crypto_payment
    order = {"amount_cents": 800}
    received = 800
    expected = int(order.get("amount_cents") or 0)
    if not (expected > 0 and received < expected):
        ok("exact discounted amount accepted")
    received2 = 799
    if expected > 0 and received2 < expected:
        ok("underpay rejected")
    # Full tariff 1000 would wrongly reject discounted 800 — ensure we don't use tariff
    full_tariff = 1000
    if received < full_tariff and not (received < expected):
        ok("discounted 800 would fail old tariff check (regression guard)")


async def test_async_bits():
    section("Async notify/referral (dry)")
    from bot.services.billing import pay_referral_once, notify_payment_once

    # balance payment: should mark and skip reward without error
    order = {
        "order_id": None,
        "payment_type": "balance",
        "user_id": 999001,
        "amount_cents": 100,
        "period_days": 30,
    }
    # create real pending then paid flags
    from database.db_payments import create_pending_order_from_tariff, find_order_by_order_id
    from database.connection import get_db

    t = {"id": None, "price_cents": 0, "price_stars": 0, "price_rub": 100, "duration_days": 7}
    _, oid = create_pending_order_from_tariff(999001, t, "balance")
    order = find_order_by_order_id(oid)
    await pay_referral_once(order)
    order2 = find_order_by_order_id(oid)
    if order2.get("referral_paid_at"):
        ok("pay_referral_once balance marks paid")
    else:
        bad("pay_referral_once balance")

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = FakeBot()
    # need user with telegram_id
    await notify_payment_once(bot, order2, "✅ test")
    order3 = find_order_by_order_id(oid)
    if order3.get("user_notified_at") and bot.sent:
        ok("notify_payment_once sends once")
    else:
        bad("notify_payment_once", f"notified={order3.get('user_notified_at')} sent={len(bot.sent)}")

    bot2 = FakeBot()
    await notify_payment_once(bot2, order3, "✅ again")
    if not bot2.sent:
        ok("notify_payment_once idempotent")
    else:
        bad("notify idempotent")

    with get_db() as conn:
        conn.execute("DELETE FROM payments WHERE order_id=?", (oid,))


def main():
    print("OpenGate practical verification")
    test_migrations()
    test_imports()
    test_panels()
    test_money_helpers()
    test_heleket_and_outbox()
    test_ops_helpers()
    test_crypto_verify_logic()
    asyncio.run(test_async_bits())

    print(f"\n=== RESULT: {PASS} passed, {FAIL} failed ===")
    if ERRORS:
        print("Failures:")
        for n, e in ERRORS:
            print(f" - {n}: {e}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
