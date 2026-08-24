# OpenGate — smoke checklist

## XUI (обязательно после каждой фазы)

- [ ] Купить тариф без промо
- [ ] Купить с percent-промо (сумма в ордере = скидочная)
- [ ] Продлить ключ
- [ ] Полная оплата балансом
- [ ] CryptoBot / Heleket check-кнопка (pending сохраняет клавиатуру)
- [ ] Webhook: `webhook_postpay_v2=0` и `=1` (без двойной рефералки)
- [ ] `/sub` / subscription sync / traffic push
- [ ] Maintenance: buy и renew блокируются; trial нет; `/maintenance on` не снимается healthcheck

## Marzban (mock / staging)

- [ ] `panel_type=marzban`, успешный test перед save
- [ ] Создание пользователя без inbound-select
- [ ] `subscription_url` в ответе / push expire+data_limit

## Naive / mieru (dry-run)

- [ ] Naive: Caddy Admin upsert или SSH users.conf merge + reload
- [ ] mieru: partial users JSON + `mita apply` (не wipe), backup `.bak`
- [ ] Ссылки `naive+https://…` / `mieru://…`
- [ ] Expiry disable/remove из бота

## Feature flags

| Setting | Default | Когда включать |
|---------|---------|----------------|
| `webhook_postpay_v2` | `0` | После smoke XUI post-pay |
| `redis_fsm_url` | пусто | MemoryStorage → Redis в maintenance window |
| `healthcheck_enabled` | `0` | После настройки серверов |
