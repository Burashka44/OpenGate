<p align="center">
  <img src="OpenGateStore_en.png" alt="OpenGate VPN Store" width="100%">
</p>

<h1 align="center">OpenGate</h1>

<p align="center">
  <b>A turnkey Telegram bot for selling VPN access: payments, instant key delivery, admin panel, referrals and promo codes.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white" alt="aiogram 3">
  <img src="https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white" alt="SQLite">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <img src="https://img.shields.io/badge/deploy-systemd-EE0000?logo=linux&logoColor=white" alt="systemd">
</p>

<p align="center">
  <a href="README.MD">Русский</a> · <b>English</b>
</p>

---

## What it is

OpenGate turns a Telegram bot into a complete VPN subscription store. A customer picks a plan,
pays with their preferred method and **receives a working key immediately** — no admin in the loop.
The bot renews subscriptions, meters traffic, pays out referral rewards and keeps the VPN panel
in sync.

The bot is the **single source of truth** for expiry dates and traffic limits: panel state is
reconciled to the database, never the other way around.

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Commands](#commands)
- [How it works](#how-it-works)
- [Operations](#operations)
- [Verifying a build](#verifying-a-build)
- [Documentation](#documentation)

## Features

### Payments

| Method | Notes |
| :--- | :--- |
| **Cardlink** (cards/SBP) | Recommended: deep-link return to the bot, automatic status check |
| **YooKassa** | Telegram Payments plus direct QR/REST with no webhooks |
| **WATA**, **Platega** | Russian acquiring: cards and SBP via QR |
| **Telegram Stars** | Native in-Telegram payments |
| **CryptoBot**, **Heleket** | Crypto with webhooks and instant crediting |
| **Custom crypto processor** | HMAC-SHA256 signature, deep-link callback |
| **Internal balance** | Top-ups, partial or full payment, auto-renewal |

### VPN panels

- **3x-UI** — primary path: purchase, renewal, subscription mode, traffic sync
- **Marzban** — user-centric REST, `subscription_url`, expiry and limit push
- **Naive** — Caddy Admin API or SSH merge of `users.conf`
- **mieru** — partial users JSON update plus `mita apply` (never a wipe)

### Sales and marketing

- **Promo codes** in four flavours: bonus days, percentage discount, balance, trial
- **Up to 3 referral levels** — days or cash rewards, per-user multipliers
- **Free trial** with an optional channel-subscription gate
- **Ad campaigns** with UTM tags and click statistics
- **Broadcasts** and subscription-expiry reminders

### Keys and traffic

- Per-plan traffic limits with **remaining quota carried over** when switching servers
- Monthly auto-reset and alerts at 10 / 5 / 3 % remaining
- Server and plan groups to keep offerings isolated from each other
- Rename, server replacement, payment history

### Administration

- Full admin panel inside Telegram: servers, plans, users, payments, texts
- **Message editor with live preview** — what you see is what the customer gets, photos included
- **Backups** daily at 03:05: ZIP to Telegram plus local `.db` files rotated for 7 days
- **Update from GitHub** inside the bot: regular, blocking (`!`) and beta (`?`) commits
- Log download and cleanup, statistics, user lookup

## Quick start

### Option 1. VPS with systemd (production)

You need an Ubuntu VPS with at least 1 vCPU / 1 GB RAM / 10 GB SSD and a VPN panel already
installed.

```bash
git clone https://github.com/Burashka44/OpenGate.git /opt/OpenGate
cd /opt/OpenGate
sudo bash install.sh
```

Choose **1) Install** and enter your `BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)) and
Telegram ID. The script creates a virtualenv, an `opengate` system user and a systemd unit.

```bash
systemctl status opengate      # status
journalctl -u opengate -f      # logs
sudo bash install.sh update    # update
```

### Option 2. Docker Compose

```bash
cp .env.example .env    # set BOT_TOKEN and ADMIN_IDS
docker compose up -d --build
docker compose logs -f
```

The database and logs persist in `./database` and `./logs` across restarts.

### Next steps

1. Open the bot and go to **Admin panel**.
2. Add a VPN server — a successful connection test is required before saving.
3. Create your plans.
4. Enable payment methods and fill in the keys listed in **[INTEGRATIONS.md](INTEGRATIONS.md)**.
5. Walk through **[SMOKE_CHECKLIST.md](SMOKE_CHECKLIST.md)** before taking real money.

> [!IMPORTANT]
> `config.py`, `.env` and the database are gitignored, so secrets never reach the repository.

## Commands

### Customer

| Command | Description |
| :--- | :--- |
| `/start` | Home screen with the welcome text and plan list |
| `/mykeys` | My keys: status, traffic, renewal |
| `/promo` | Redeem a promo code |
| `/topup` | Top up the internal balance |
| `/help` | Help page |

### Administrator

| Command | Description |
| :--- | :--- |
| `/ops` | Summary of operational settings |
| `/ops_set KEY VALUE` | Change a whitelisted setting |
| `/maintenance on\|off` | Maintenance mode: blocks purchases and renewals |
| `/promo_add`, `/promo_list`, `/promo_del` | Manage promo codes |
| `/promo_on`, `/promo_off` | Enable or disable a promo code |
| `/ad_add`, `/ad_list`, `/ad_del` | Ad campaigns and UTM tracking |
| `/update` | Pull and deploy code from GitHub |

> [!TIP]
> Only register the customer commands in [@BotFather](https://t.me/BotFather) — admin commands
> do not belong in the public menu.

## How it works

```
Telegram ──> aiogram 3 (polling) ──> SQLite (WAL, auto-migrations)
                  │                        │
                  │                        └─> keys, payments, promos, referrals
                  ├─> payment APIs ──> confirmation ──> key delivery
                  ├─> VPN panel (3x-UI / Marzban / Naive / mieru)
                  └─> aiohttp: /sub, webhooks, /healthz
```

Safeguards around the money path:

- **Idempotency** — a repeated webhook or "I paid" tap never issues a key or referral twice
- **Webhook outbox** — outgoing events with retries
- **Circuit breaker** — a failing panel is temporarily taken out of rotation
- **Panel healthcheck** with automatic maintenance mode if every panel is down

## Operations

The built-in web server is disabled by default. Enable it with `web_enabled=1` (port `web_port`,
exposed publicly through nginx or caddy):

| Route | Purpose |
| :--- | :--- |
| `/healthz` | Liveness probe |
| `/sub/{token}` | Branded subscription page |
| `/webhook/cryptobot`, `/webhook/heleket` | Payment webhook intake |

FSM storage can optionally move to Redis — set `redis_fsm_url` so dialog state survives restarts.

## Verifying a build

Automated checks cover the database schema, critical imports, money arithmetic, webhook
signatures and the circuit breaker.

```bash
python verify_audit.py        # integration checks
python verify_audit_deep.py   # discounts, orders, idempotency
python -m pytest tests -q     # panel mocks
```

## Documentation

| File | Contents |
| :--- | :--- |
| **[ADMIN_GUIDE.md](ADMIN_GUIDE.md)** | Manual install, admin panel, backups, sync, editable texts |
| **[INTEGRATIONS.md](INTEGRATIONS.md)** | Every external service, `settings` keys, first-run checklist |
| **[SMOKE_CHECKLIST.md](SMOKE_CHECKLIST.md)** | What to test before going live |

---

<p align="center">
  <sub>Before launching publicly, replace the demo branding, donation links and
  <code>GITHUB_REPO_URL</code> with your own — see the branding section in INTEGRATIONS.md.</sub>
</p>
