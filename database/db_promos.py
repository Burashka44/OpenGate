"""
Промокоды: CRUD, валидация и активация.

Типы промокодов (promo_type):
- 'days'    — немедленно добавляет N дней к первому активному ключу
              (или к следующей покупке, если ключей нет — через users.next_discount... нет,
              просто отклоняем, если нет активного ключа)
- 'percent' — скидка N% на следующую покупку (users.next_discount_percent)
- 'balance' — немедленно зачисляет N копеек на баланс
- 'trial'   — сбрасывает used_trial (разблокирует пробную подписку)
"""
import logging
from typing import Optional, List, Dict, Any, Tuple
from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = [
    'create_promo_code',
    'get_promo_by_code',
    'get_all_promo_codes',
    'set_promo_active',
    'delete_promo_code',
    'count_user_activations',
    'activate_promo',
    'get_promo_stats',
    'get_user_next_discount',
    'set_user_next_discount',
    'consume_user_discount',
]

VALID_PROMO_TYPES = ('days', 'percent', 'balance', 'trial')


def create_promo_code(
    code: str,
    promo_type: str,
    value: int,
    max_uses: int = 0,
    per_user_limit: int = 1,
    expires_at: Optional[str] = None,
) -> Optional[int]:
    """
    Создаёт промокод.

    Args:
        code: Код (без пробелов, регистронезависимый)
        promo_type: 'days' | 'percent' | 'balance' | 'trial'
        value: Значение (дни / проценты / копейки / игнорируется для trial)
        max_uses: Максимум активаций всего (0 = безлимит)
        per_user_limit: Максимум активаций на пользователя
        expires_at: ISO-дата истечения или None

    Returns:
        ID промокода или None при дубликате
    """
    if promo_type not in VALID_PROMO_TYPES:
        raise ValueError(f"Неверный тип промокода: {promo_type}")
    try:
        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO promo_codes (code, promo_type, value, max_uses, per_user_limit, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (code.strip(), promo_type, value, max_uses, per_user_limit, expires_at))
            logger.info(f"Создан промокод {code} ({promo_type}={value})")
            return cursor.lastrowid
    except Exception as e:
        if 'UNIQUE' in str(e):
            return None
        raise


def get_promo_by_code(code: str) -> Optional[Dict[str, Any]]:
    """Находит промокод по коду (регистронезависимо)."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT * FROM promo_codes WHERE code = ? COLLATE NOCASE",
            (code.strip(),)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_promo_codes() -> List[Dict[str, Any]]:
    """Все промокоды с количеством активаций."""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT p.*, COUNT(a.id) as activations
            FROM promo_codes p
            LEFT JOIN promo_activations a ON a.promo_id = p.id
            GROUP BY p.id
            ORDER BY p.id DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def set_promo_active(promo_id: int, active: bool) -> bool:
    """Включает/выключает промокод."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE promo_codes SET is_active = ? WHERE id = ?",
            (1 if active else 0, promo_id)
        )
        return cursor.rowcount > 0


def delete_promo_code(promo_id: int) -> bool:
    """Удаляет промокод вместе с журналом активаций (CASCADE)."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
        return cursor.rowcount > 0


def count_user_activations(promo_id: int, user_id: int) -> int:
    """Сколько раз пользователь активировал этот промокод."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM promo_activations WHERE promo_id = ? AND user_id = ?",
            (promo_id, user_id)
        )
        return cursor.fetchone()['cnt']


def activate_promo(code: str, user_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Валидирует и активирует промокод для пользователя.

    Атомарно: инкремент used_count с проверкой лимита в одном UPDATE,
    затем запись активации. Применение эффекта — на вызывающей стороне
    (bot.services.billing.apply_promo_effect).

    Returns:
        (success, message, promo_dict)
    """
    promo = get_promo_by_code(code)
    if not promo or not promo['is_active']:
        return False, "❌ Промокод не найден или отключён.", None

    with get_db() as conn:
        # Истечение по дате
        cursor = conn.execute("""
            SELECT 1 FROM promo_codes
            WHERE id = ? AND (expires_at IS NULL OR expires_at > datetime('now'))
        """, (promo['id'],))
        if not cursor.fetchone():
            return False, "❌ Срок действия промокода истёк.", None

        # Лимит на пользователя
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM promo_activations WHERE promo_id = ? AND user_id = ?",
            (promo['id'], user_id)
        )
        if cursor.fetchone()['cnt'] >= (promo['per_user_limit'] or 1):
            return False, "❌ Вы уже использовали этот промокод.", None

        # Атомарный инкремент с проверкой общего лимита
        cursor = conn.execute("""
            UPDATE promo_codes
            SET used_count = used_count + 1
            WHERE id = ? AND (max_uses = 0 OR used_count < max_uses)
        """, (promo['id'],))
        if cursor.rowcount == 0:
            return False, "❌ Лимит активаций промокода исчерпан.", None

        conn.execute(
            "INSERT INTO promo_activations (promo_id, user_id) VALUES (?, ?)",
            (promo['id'], user_id)
        )

    logger.info(f"Промокод {promo['code']} активирован пользователем {user_id}")
    return True, "", promo


def get_promo_stats(promo_id: int) -> Dict[str, Any]:
    """Статистика активаций промокода."""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT COUNT(*) as total, COUNT(DISTINCT user_id) as unique_users
            FROM promo_activations WHERE promo_id = ?
        """, (promo_id,))
        return dict(cursor.fetchone())


def get_user_next_discount(user_id: int) -> int:
    """Персональная скидка (%) на следующую покупку."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT next_discount_percent FROM users WHERE id = ?", (user_id,)
        )
        row = cursor.fetchone()
        return (row['next_discount_percent'] or 0) if row else 0


def set_user_next_discount(user_id: int, percent: int) -> None:
    """Устанавливает скидку на следующую покупку."""
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET next_discount_percent = ? WHERE id = ?",
            (max(0, min(99, percent)), user_id)
        )


def consume_user_discount(user_id: int) -> None:
    """Сжигает скидку после успешной оплаты."""
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET next_discount_percent = 0 WHERE id = ?",
            (user_id,)
        )
