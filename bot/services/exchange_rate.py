"""
Сервис курсов валют.

Получение курса USD/RUB от ЦБ РФ с fallback в settings.
"""
import logging
import aiohttp

from database.requests import get_setting, set_setting

logger = logging.getLogger(__name__)

DEFAULT_USD_RUB_RATE = '10000'


async def get_usd_rub_rate() -> int:
    """
    Получить курс USD/RUB в копейках.
    Сначала пробует ЦБ РФ, при ошибке берёт из settings (fallback).

    Returns:
        Курс USD/RUB в копейках (например, 9500 = 95.00 руб)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://www.cbr-xml-daily.ru/daily_json.js',
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json(content_type=None)
                rate = data['Valute']['USD']['Value']
                rate_cents = int(rate * 100)
                set_setting('usd_rub_rate', str(rate_cents))
                return rate_cents
    except Exception as e:
        logger.error(f"Failed to get exchange rate from CB: {e}")
        val = get_setting('usd_rub_rate', DEFAULT_USD_RUB_RATE)
        try:
            return int(val)
        except (ValueError, TypeError):
            logger.error(f"Некорректное значение курса в settings: {val}")
            return int(DEFAULT_USD_RUB_RATE)
