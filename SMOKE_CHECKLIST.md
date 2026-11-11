# OpenGate — smoke checklist

## 3x-UI / LucX (обязательно после каждой фазы)

- [ ] Купить тариф без промо → пользователь получает **subscription URL**
- [ ] Купить с percent-промо (сумма в ордере = скидочная)
- [ ] Продлить ключ
- [ ] Полная оплата балансом
- [ ] CryptoBot / Heleket check-кнопка (pending сохраняет клавиатуру)
- [ ] Webhook: `webhook_postpay_v2=0` и `=1` (без двойной рефералки)
- [ ] `/sub` / subscription sync / traffic push
- [ ] Maintenance: buy и renew блокируются; trial нет; `/maintenance on` не снимается healthcheck
- [ ] `panel_type=xui`: в подписке только протоколы 3x (VLESS/Hy2/…)
- [ ] `panel_type=lucx`: в подписке также mieru/AWG/… (что настроено на панели)
- [ ] LucX: клиент создаётся через `/panel/api/clients/add` + attach ко всем inbound

## Feature flags

| Setting | Default | Когда включать |
|---------|---------|----------------|
| `webhook_postpay_v2` | `0` | После smoke XUI post-pay |
| `redis_fsm_url` | пусто | MemoryStorage → Redis в maintenance window |
| `healthcheck_enabled` | `0` | После настройки серверов |
