"""
Webhook outbox: идемпотентная очередь по provider event id + retry.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Dict, Any, List

from database.connection import get_db

logger = logging.getLogger(__name__)


def enqueue_webhook_event(
    provider: str,
    event_id: str,
    order_id: str,
    payload: Optional[dict] = None,
) -> bool:
    """
    Кладёт событие в outbox. False если event_id уже есть (unique).
    """
    with get_db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO webhook_outbox (provider, event_id, order_id, payload, status, attempts)
                VALUES (?, ?, ?, ?, 'pending', 0)
                """,
                (provider, event_id, order_id, json.dumps(payload or {}, ensure_ascii=False)),
            )
            return True
        except Exception:
            logger.info(f"webhook outbox duplicate: {provider}/{event_id}")
            return False


def claim_pending(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM webhook_outbox
            WHERE status = 'pending' AND attempts < 10
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_done(row_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE webhook_outbox SET status='done', processed_at=CURRENT_TIMESTAMP WHERE id=?",
            (row_id,),
        )


def mark_retry(row_id: int, error: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE webhook_outbox
            SET attempts = attempts + 1,
                last_error = ?,
                status = CASE WHEN attempts + 1 >= 10 THEN 'failed' ELSE 'pending' END
            WHERE id = ?
            """,
            (error[:500], row_id),
        )


async def process_outbox(bot=None) -> int:
    """Обрабатывает pending outbox. Возвращает число успешных."""
    from bot.services.billing import fulfill_paid_order, pay_referral_once, notify_payment_once
    from database.requests import get_setting

    use_v2 = get_setting('webhook_postpay_v2', '0') == '1'
    done = 0
    for row in claim_pending():
        order_id = row['order_id']
        try:
            if use_v2:
                success, text, order = await fulfill_paid_order(order_id)
                if success and order:
                    await pay_referral_once(order)
                    if bot:
                        await notify_payment_once(bot, order, text)
            else:
                from bot.services.billing import process_payment_order
                success, text, order = await process_payment_order(order_id)
            if success:
                mark_done(row['id'])
                done += 1
            else:
                mark_retry(row['id'], text or 'not success')
        except Exception as e:
            logger.exception(f"outbox process id={row['id']}: {e}")
            mark_retry(row['id'], str(e))
    return done
