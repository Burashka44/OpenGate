"""
UTM / рекламные кампании: ссылки вида https://t.me/{bot}?start=ad_{code}.

Метрики воронки считаются по users.campaign_id:
регистрации → trial → первая оплата.
"""
import logging
from typing import Optional, List, Dict, Any
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'create_campaign',
    'get_campaign_by_code',
    'get_all_campaigns_with_stats',
    'delete_campaign',
    'assign_user_campaign',
]


def create_campaign(code: str, name: str) -> Optional[int]:
    """Создаёт кампанию. Возвращает id или None при дубликате кода."""
    try:
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO campaigns (code, name) VALUES (?, ?)",
                (code.strip(), name.strip())
            )
            logger.info(f"Создана кампания {code} ({name})")
            return cursor.lastrowid
    except Exception as e:
        if 'UNIQUE' in str(e):
            return None
        raise


def get_campaign_by_code(code: str) -> Optional[Dict[str, Any]]:
    """Кампания по коду (регистронезависимо)."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT * FROM campaigns WHERE code = ? COLLATE NOCASE AND is_active = 1",
            (code.strip(),)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_campaigns_with_stats() -> List[Dict[str, Any]]:
    """Все кампании с воронкой: регистрации / trial / оплатившие."""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT
                c.id, c.code, c.name, c.is_active, c.created_at,
                COUNT(u.id) as registrations,
                SUM(CASE WHEN u.used_trial = 1 THEN 1 ELSE 0 END) as trials,
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM payments p
                    WHERE p.user_id = u.id AND p.status = 'paid'
                ) THEN 1 ELSE 0 END) as payers
            FROM campaigns c
            LEFT JOIN users u ON u.campaign_id = c.id
            GROUP BY c.id
            ORDER BY c.id DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def delete_campaign(campaign_id: int) -> bool:
    """Удаляет кампанию (у пользователей campaign_id остаётся, FK без каскада)."""
    with get_db() as conn:
        conn.execute("UPDATE users SET campaign_id = NULL WHERE campaign_id = ?", (campaign_id,))
        cursor = conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        return cursor.rowcount > 0


def assign_user_campaign(user_id: int, campaign_id: int) -> None:
    """Привязывает пользователя к кампании (только если ещё не привязан)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET campaign_id = ? WHERE id = ? AND campaign_id IS NULL",
            (campaign_id, user_id)
        )
