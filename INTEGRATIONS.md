# OpenGate — карта интеграций

Перед продакшеном заполните ключи ниже. Пустые значения = функция скрыта или «не настроено».

## Таблица сервисов

| Интеграция | Зачем | Где регистрироваться | Ключи `settings` / `config.py` | Где в админ-боте |
|------------|-------|----------------------|--------------------------------|------------------|
| Telegram Bot | сам бот | @BotFather | `config.py` → `BOT_TOKEN`, `ADMIN_IDS` | — |
| 3x-UI | VPN-панель (default `panel_type=xui`) | VPS + 3x-ui | таблица `servers` | Админ → Сервера |
| Marzban | VPN-панель (`panel_type=marzban`) | VPS + Marzban | `servers` + JWT login | Админ → Сервера |
| Naive | NaiveProxy (`panel_type=naive`) | Caddy + Naive | `public_host`, `extra_config.caddy_admin_url` или SSH | Админ → Сервера |
| mieru | mieru/mita (`panel_type=mieru`) | mita | `extra_config` apply/reload, merge-only | Админ → Сервера |
| Telegram Stars | оплата звёздами | BotFather → Payments | `stars_enabled` | Админ → Оплаты |
| ЮKassa QR | карты/СБП напрямую | [yookassa.ru](https://yookassa.ru/) | `yookassa_shop_id`, `yookassa_secret_key` | Админ → Оплаты |
| WATA | карты/СБП | [wata.pro](https://wata.pro/) | `wata_jwt_token` | Админ → Оплаты → WATA |
| Platega | СБП | [app.platega.io](https://app.platega.io/) | `platega_merchant_id`, `platega_secret` | Админ → Оплаты → Platega |
| Cardlink | карты/СБП | [cardlink.link](https://cardlink.link/) | `cardlink_shop_id`, `cardlink_api_token`, `cardlink_partner_uuid` (опц.) | Админ → Оплаты → Cardlink |
| Крипто-процессор | USDT/TON и др. | свой Telegram-бот | `crypto_processor_url`, `crypto_item_url`, `crypto_secret_key`, `crypto_enabled` | Админ → Оплаты → Крипто |
| Поддержка пользователей | чат | свой канал/чат | `support_channel_link` | Редактор страниц / help |
| Новости | канал | свой канал | кнопка `btn_news` на странице help | Редактор страниц |
| Поддержка проекта | донаты | YooMoney, крипто-кошелёк | `project_support_*` | Админ → Поддержка проекта |
| GitHub updates | обновление кода | ваш репозиторий | `GITHUB_REPO_URL` в `config.py` | Админ → Настройки бота |
| Реферальная программа | маркетинг | — | встроено в БД | Админ → Рефералы |

## Ключи «Поддержка проекта» (`project_support_*`)

| Ключ | Назначение |
|------|------------|
| `project_support_title` | Заголовок экрана (HTML) |
| `project_support_text` | Основной текст |
| `project_support_donation_card_url` | Ссылка «Карты РФ» |
| `project_support_donation_crypto_url` | Ссылка «Крипто» |
| `project_support_extra_url` | Доп. кнопка (опционально) |
| `project_support_extra_label` | Подпись доп. кнопки |

Запись: `INSERT INTO settings (key, value) VALUES ('project_support_title', 'Поддержка проекта');` или через будущий редактор в админке.

## Крипто: протокол callback

- Пользователь переходит по ссылке `{crypto_processor_url}?start=item-{id}-...-{invoice}-...`
- После оплаты процессор шлёт deep-link вида `bill1-...` с HMAC-подписью (первые 11 байт HMAC-SHA256 → Base62).
- `crypto_secret_key` — ключ для проверки подписи.
- `crypto_processor_url` можно не задавать: при сохранении `crypto_item_url` база выводится автоматически (часть до `?start=`).

## Cardlink partner UUID

- `cardlink_partner_uuid` — опционально, ваша партнёрская программа.
- Если пусто, поле `partner_uuid` в API не отправляется.

## Чеклист первого запуска

1. Создать бота в @BotFather, записать `BOT_TOKEN` и свой ID в `ADMIN_IDS`.
2. Развернуть на VPS: `sudo bash install.sh` (см. README) или ручная установка из ADMIN_GUIDE.
3. Добавить VPN-сервер в админке → Сервера (`panel_type`: `xui` / `marzban` / `naive` / `mieru`). Тест подключения обязателен перед save.
4. Создать тарифы; для naive/mieru задать `protocol_family` соответственно (xray по умолчанию → xui+marzban).
5. Включить нужные способы оплаты и заполнить ключи по таблице выше.
6. Настроить `support_channel_link` и кнопки help (новости/поддержка).
7. Заполнить `project_support_*`, если нужен экран доната.
8. Указать `GITHUB_REPO_URL` на свой репозиторий OpenGate для `/update`.
9. Проверить пробную подписку и рефералку.
10. Пройти тестовую оплату каждым включённым методом.

## Брендинг перед продакшеном

Замените демо-значения на свои:

| Что проверить | Setting / действие |
|---------------|--------------------|
| Донаты / крипто-кошелёк | `project_support_donation_*` |
| Cardlink partner UUID | `cardlink_partner_uuid` |
| URL крипто-процессора | `crypto_processor_url` + `crypto_item_url` |
| Каналы в help | `support_channel_link`, кнопки help |
| Источник обновлений | свой `REPO_URL` в `install.sh` и `GITHUB_REPO_URL` в `config.py` |
