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


def reclaim_stale_processing(minutes: int = 5) -> int:
    """Возвращает зависшие processing → pending (после рестарта / таймаута)."""
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE webhook_outbox
            SET status = 'pending', claimed_at = NULL
            WHERE status = 'processing'
              AND (
                    claimed_at IS NULL
                    OR datetime(claimed_at) < datetime('now', ?)
                  )
            """,
            (f'-{int(minutes)} minutes',),
        )
        return cursor.rowcount or 0


def claim_pending(limit: int = 20) -> List[Dict[str, Any]]:
    """Атомарно захватывает pending → processing."""
    from database.connection import get_connection

    reclaim_stale_processing(5)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id FROM webhook_outbox
            WHERE status = 'pending' AND attempts < 10
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        ids = [int(r['id']) for r in rows]
        if not ids:
            conn.commit()
            return []
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"""
            UPDATE webhook_outbox
            SET status = 'processing', claimed_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders}) AND status = 'pending'
            """,
            ids,
        )
        claimed = conn.execute(
            f"""
            SELECT * FROM webhook_outbox
            WHERE id IN ({placeholders}) AND status = 'processing'
            """,
            ids,
        ).fetchall()
        conn.commit()
        return [dict(r) for r in claimed]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_done(row_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE webhook_outbox
            SET status='done', processed_at=CURRENT_TIMESTAMP, claimed_at=NULL
            WHERE id=?
            """,
            (row_id,),
        )


def mark_retry(row_id: int, error: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE webhook_outbox
            SET attempts = attempts + 1,
                last_error = ?,
                claimed_at = NULL,
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
