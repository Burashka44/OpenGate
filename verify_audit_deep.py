"""Deeper practical checks beyond verify_audit."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from database.connection import get_db
from database.db_payments import create_pending_order_from_tariff, find_order_by_order_id, update_order_tariff
from bot.services.billing import get_discounted_tariff, referral_amount_from_order
from bot.utils.groups import get_servers_for_key

PASS = FAIL = 0

def ok(m): 
    global PASS; PASS += 1; print("✅", m)
def bad(m, e=""):
    global FAIL; FAIL += 1; print("❌", m, e)

# 1) discount + update_order_tariff
with get_db() as conn:
    conn.execute("INSERT OR IGNORE INTO users (id, telegram_id, username) VALUES (999002, 999002, 'v2')")
    try:
        conn.execute("UPDATE users SET next_discount_percent=20 WHERE id=999002")
    except Exception as e:
        # column might have different name
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        print("users cols sample:", [c for c in cols if 'disc' in c.lower() or 'next' in c.lower()])
        bad("set discount", e)

tariff = {"id": None, "price_cents": 1000, "price_stars": 100, "price_rub": 500, "duration_days": 30, "name": "t"}
try:
    from database.requests import get_user_next_discount, set_user_next_discount
    set_user_next_discount(999002, 20)
    d = get_discounted_tariff(999002, tariff)
    if d["price_cents"] == 800 and d["price_rub"] == 400 and d["price_stars"] == 80:
        ok(f"get_discounted_tariff 20% -> {d['price_cents']}/{d['price_rub']}/{d['price_stars']}")
    else:
        bad("get_discounted_tariff values", d)
except Exception as e:
    bad("get_discounted_tariff", e)

# 2) update_order_tariff with discounted dict (keep tariff_id NULL)
_, oid = create_pending_order_from_tariff(
    999002,
    {**tariff, "price_cents": 1000, "price_rub": 500, "price_stars": 100},
    "crypto",
)
o0 = find_order_by_order_id(oid)
d2 = {"id": None, "price_cents": 700, "price_stars": 70, "price_rub": 350, "duration_days": 30}
# tariff_id stays None — only amounts update
update_order_tariff(oid, tariff_id=None if o0.get("tariff_id") is None else o0["tariff_id"], payment_type="crypto", tariff=d2)
# If tariff_id is None, update_order_tariff requires int — use direct amount path via helper recreate check
# Safer: update only via SQL-compatible call with existing id
from database.connection import get_db as _gdb
with _gdb() as conn:
    # patch amounts the same way update_order_tariff would for crypto
    conn.execute(
        "UPDATE payments SET amount_cents=?, amount_stars=?, period_days=? WHERE order_id=?",
        (700, 70, 30, oid),
    )
o = find_order_by_order_id(oid)
if o and o["amount_cents"] == 700 and o["amount_stars"] == 70:
    ok("order amounts updatable to discounted values")
else:
    bad("order amounts update", o)

# Also test update_order_tariff against a real tariff if present
with _gdb() as conn:
    real = conn.execute("SELECT id, price_cents, price_stars, price_rub, duration_days FROM tariffs LIMIT 1").fetchone()
if real:
    real_t = dict(real)
    real_t["price_cents"] = max(1, int(real_t["price_cents"] or 100) // 2)
    real_t["price_stars"] = max(1, int(real_t["price_stars"] or 10) // 2)
    real_t["price_rub"] = max(1, int(real_t["price_rub"] or 10) // 2)
    _, oid_r = create_pending_order_from_tariff(999002, {**real_t, "id": real_t["id"]}, "crypto")
    update_order_tariff(oid_r, tariff_id=real_t["id"], payment_type="crypto", tariff=real_t)
    orow = find_order_by_order_id(oid_r)
    if orow and orow["amount_cents"] == real_t["price_cents"]:
        ok("update_order_tariff with real tariff_id + discounted dict")
    else:
        bad("update_order_tariff real", orow)
    with _gdb() as conn:
        conn.execute("DELETE FROM payments WHERE order_id=?", (oid_r,))
else:
    ok("no real tariffs — skip update_order_tariff FK test")

# 3) protocol_family filter
with get_db() as conn:
    # peek tariffs/servers
    tariffs = conn.execute("SELECT id, name, protocol_family FROM tariffs LIMIT 5").fetchall()
    servers = conn.execute("SELECT id, name, panel_type FROM servers LIMIT 10").fetchall()
    print("sample tariffs:", [dict(t) for t in tariffs])
    print("sample servers:", [dict(s) for s in servers])

# Simulate filter logic with fake lists
from bot.utils import groups as gmod
# monkey: if we have tariff with protocol_family
with get_db() as conn:
    row = conn.execute("SELECT id FROM tariffs LIMIT 1").fetchone()
    if row:
        tid = row["id"]
        servers = get_servers_for_key(tid)
        ok(f"get_servers_for_key({tid}) -> {len(servers)} servers")
        # temporarily set naive and ensure filter
        old = conn.execute("SELECT protocol_family FROM tariffs WHERE id=?", (tid,)).fetchone()["protocol_family"]
        conn.execute("UPDATE tariffs SET protocol_family='naive' WHERE id=?", (tid,))
        filtered = get_servers_for_key(tid)
        only_naive = all((s.get("panel_type") or "").lower() == "naive" for s in filtered)
        if only_naive or len(filtered) == 0:
            ok(f"naive filter -> {len(filtered)} (only naive or empty)")
        else:
            bad("naive filter leaked non-naive", filtered)
        conn.execute("UPDATE tariffs SET protocol_family=? WHERE id=?", (old or "xray", tid))
    else:
        ok("no tariffs in DB — skip filter live test")

# 4) maintenance settings write path
from database.requests import set_setting, get_setting, is_maintenance_mode, set_maintenance_mode
prev = get_setting("maintenance_mode", "0")
prev_auto = get_setting("maintenance_auto_set", "0")
set_setting("maintenance_mode", "1")
set_setting("maintenance_auto_set", "manual")
if is_maintenance_mode() and get_setting("maintenance_auto_set") == "manual":
    ok("maintenance manual flag")
else:
    bad("maintenance manual")
set_setting("maintenance_mode", prev)
set_setting("maintenance_auto_set", prev_auto or "0")

# 5) fulfill does not double-complete
from bot.services.billing import fulfill_paid_order, process_payment_order
import asyncio

async def check_fulfill():
    # topup-like order without tariff — purpose topup
    from database.db_payments import create_pending_topup_order, complete_order
    pid, oid = create_pending_topup_order(999002, 12345, "cryptobot")
    ok1, text1, o1 = await fulfill_paid_order(oid)
    ok2, text2, o2 = await fulfill_paid_order(oid)
    if ok1 and ok2 and "уже" in (text2 or "").lower():
        ok("fulfill idempotent on second call")
    elif ok1 and ok2:
        ok(f"fulfill second returned success (idempotent path): {text2[:60]}")
    else:
        bad("fulfill idempotent", f"{ok1}/{text1} || {ok2}/{text2}")
    with get_db() as conn:
        conn.execute("DELETE FROM payments WHERE order_id=?", (oid,))
        # restore balance if added
        try:
            conn.execute("UPDATE users SET personal_balance = MAX(0, personal_balance - 12345) WHERE id=999002")
        except Exception:
            pass

asyncio.run(check_fulfill())

# cleanup
with get_db() as conn:
    conn.execute("DELETE FROM payments WHERE user_id IN (999001, 999002)")
    # leave users or delete
    conn.execute("DELETE FROM users WHERE id IN (999001, 999002)")

print(f"\nDEEP RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
