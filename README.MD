<p align="center">
  <img src="OpenGate.png" alt="OpenGate VPN Bot" width="100%">
</p>

<h1 align="center">OpenGate — Telegram-бот для VPN-бизнеса (3x-UI / Marzban)</h1>

<p align="center">
  <b>Автоматизация продажи VPN в Telegram: оплата, выдача ключей, админка, рефералка.</b>
</p>

<p align="center">
  <i>Python · aiogram 3 · SQLite · systemd · ЮKassa · Cardlink · WATA · Platega · Stars · крипто</i>
</p>

<p align="center">
  <a href="#1--сервер-vps">🚀 Сервер</a> •
  <a href="#2--установка">🛠️ Установка</a> •
  <a href="#3--настройка">⚙️ Настройка</a> •
  <a href="ADMIN_GUIDE.md">📖 Руководство</a> •
  <a href="INTEGRATIONS.md">🔌 Интеграции</a>
</p>

---

## ✨ Возможности

| VPN и админка | Платежи |
| :--- | :--- |
| 3x-UI и Marzban, группы серверов/тарифов | ЮKassa, Cardlink, WATA, Platega |
| Рефералка до 3 уровней, триал, рассылки | Telegram Stars, крипто (bill+HMAC) |
| Subscription mode, управление ключами | Внутренний баланс, автовыдача |

Подробная настройка платёжек и ссылок: **[INTEGRATIONS.md](INTEGRATIONS.md)**.

---

## 1. 🚀 Сервер (VPS)

1. VPS с **Ubuntu**, минимум 1 vCPU / 1 GB RAM / 10 GB SSD.
2. Установите панель **3x-UI** или **Marzban** на сервере.
3. Сохраните URL панели, логин и пароль — они понадобятся в админке бота.

Хостинг выбирайте сами (без привязки к реф-ссылкам).

---

## 2. 🛠️ Установка

На сервере (от root):

```bash
git clone https://github.com/Burashka44/OpenGate.git /opt/OpenGate
cd /opt/OpenGate
# Отредактируйте REPO_URL в install.sh при необходимости
sudo bash install.sh
```

Выберите **1) Установка**, введите `BOT_TOKEN` и `ADMIN_ID`.

Сервис: `systemctl status opengate` (пользователь `opengate`, каталог `/opt/OpenGate`).

Обновление: повторный запуск `install.sh` → пункты 2 (мягкое) или 3 (жёсткое).

Ручная установка: **[ADMIN_GUIDE.md](ADMIN_GUIDE.md)**.

---

## 3. ⚙️ Настройка

1. Откройте бота → **Админ-панель**.
2. Добавьте сервер и тарифы.
3. По **[INTEGRATIONS.md](INTEGRATIONS.md)** включите оплаты и впишите ключи.
4. Настройте тексты help и `support_channel_link`.


---

## 📖 Документация

- **[ADMIN_GUIDE.md](ADMIN_GUIDE.md)** — команды, админка, бэкапы.
- **[INTEGRATIONS.md](INTEGRATIONS.md)** — все внешние сервисы и ключи settings.ap.
