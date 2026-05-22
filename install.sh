#!/bin/bash
# Self-fix: перезапуск с LF, если файл попал с Windows (CRLF)
if LC_ALL=C grep -q $'\r' "$0" 2>/dev/null; then
    sed -i 's/\r$//' "$0"
    exec bash "$0" "$@"
fi

# OpenGate — скрипт установки и управления
# Локальный запуск из каталога проекта (рекомендуется):
#   sudo bash install.sh
#   sudo bash install.sh install <BOT_TOKEN> <ADMIN_ID>
#   sudo bash install.sh update [COMMIT_OR_BRANCH]
#   sudo bash install.sh reset [COMMIT_OR_BRANCH]
#
# Перед установкой укажите REPO_URL на ваш форк OpenGate (см. ниже).
#
# 2. Локальный запуск (если репозиторий уже установлен и нужно просто обновить/сбросить):
# bash install.sh update [COMMIT_OR_BRANCH]
# bash install.sh reset [COMMIT_OR_BRANCH]

set -e

INSTALL_DIR="/opt/OpenGate"
REPO_URL="https://github.com/Burashka44/OpenGate.git"
RUN_USER="opengate"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="opengate"
SERVICE_FILE="opengate.service"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_header() {
    echo -e "\n${CYAN}========================================${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}========================================${NC}\n"
}

print_ok() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_err() {
    echo -e "${RED}[✗]${NC} $1"
}

# Убрать CRLF в shell-скриптах (защита при клоне с Windows или старого кэша)
# Текущая оболочка не должна оставаться в INSTALL_DIR перед rm/git clone
safe_cd_away_from_install_dir() {
    case "$PWD" in
        "$INSTALL_DIR"|"$INSTALL_DIR"/*)
            cd / 2>/dev/null || cd /opt 2>/dev/null || cd /tmp
            ;;
    esac
}

normalize_repo_files() {
    local dir="${1:-$INSTALL_DIR}"
    if [ ! -d "$dir" ]; then
        return 0
    fi
    find "$dir" -type f \( -name '*.sh' -o -name '*.service' \) -print0 2>/dev/null \
        | while IFS= read -r -d '' f; do
            sed -i 's/\r$//' "$f"
        done
    if [ -d "$dir/.git" ]; then
        git -C "$dir" config core.autocrlf false 2>/dev/null || true
        git -C "$dir" config core.eol lf 2>/dev/null || true
    fi
}

# Запрос настроек у пользователя
ask_config() {
    print_header "Настройка конфигурации"

    if [ "$AUTO_MODE" = "1" ]; then
        NEED_WRITE_CONFIG=1
        print_ok "Автоматический режим: используем переданные параметры"
        return 0
    fi

    if [ -f "$INSTALL_DIR/config.py" ]; then
        echo -e "${YELLOW}Обнаружен существующий config.py${NC}"
        read -p "Использовать существующие настройки? (Y/n): " use_existing
        use_existing=${use_existing:-Y}
        if [[ "$use_existing" =~ ^[YyДд]$ ]]; then
            print_ok "Используем существующий config.py"
            return 0
        fi
    fi

    echo ""
    echo -e "${CYAN}Введите данные для настройки бота:${NC}"
    echo ""

    while true; do
        read -p "BOT_TOKEN (от @BotFather): " bot_token
        if [ -n "$bot_token" ]; then
            break
        fi
        print_err "BOT_TOKEN не может быть пустым!"
    done

    while true; do
        read -p "ADMIN_IDS (ваш Telegram ID): " admin_id
        if [ -n "$admin_id" ] && [[ "$admin_id" =~ ^[0-9]+$ ]]; then
            break
        fi
        print_err "ADMIN_IDS должен быть числом!"
    done

    BOT_TOKEN="$bot_token"
    ADMIN_ID="$admin_id"
    NEED_WRITE_CONFIG=1
    print_ok "Данные получены"
}

# Создание/обновление config.py
write_config() {
    if [ "$NEED_WRITE_CONFIG" != "1" ]; then
        return 0
    fi

    cp "$INSTALL_DIR/config.py.example" "$INSTALL_DIR/config.py"

    sed -i "s|\"ВАШ_ТОКЕН_БОТА\"|\"$BOT_TOKEN\"|g" "$INSTALL_DIR/config.py"
    sed -i "s|12345678|$ADMIN_ID|g" "$INSTALL_DIR/config.py"

    print_ok "config.py создан с вашими настройками"
}

# Установка системных пакетов
install_system_deps() {
    print_header "Установка системных зависимостей"

    export DEBIAN_FRONTEND=noninteractive
    export NEEDRESTART_MODE=a

    apt-get update -qq
    apt-get install -y -qq \
        python3-venv \
        python3-pip \
        git \
        > /dev/null 2>&1

    print_ok "Системные пакеты обновлены"
    print_ok "python3-venv, python3-pip, git установлены"
}

# Создание виртуального окружения и установка зависимостей
setup_venv() {
    print_header "Настройка виртуального окружения Python"

    python3 -m venv "$VENV_DIR"
    print_ok "Виртуальное окружение создано: $VENV_DIR"

    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    pip install --upgrade -r "$INSTALL_DIR/requirements.txt" -q
    deactivate

    print_ok "Зависимости Python установлены в venv"
}

# Системный пользователь opengate (не root)
ensure_system_user() {
    if ! id -u "$RUN_USER" &>/dev/null; then
        useradd -r -m -d /home/opengate -s /usr/sbin/nologin "$RUN_USER" 2>/dev/null \
            || useradd -r -s /usr/sbin/nologin "$RUN_USER"
        print_ok "Создан пользователь $RUN_USER"
    else
        print_ok "Пользователь $RUN_USER уже существует"
    fi
}

# Настройка systemd сервиса
setup_systemd() {
    print_header "Настройка автозапуска (systemd)"

    if [ ! -f "$INSTALL_DIR/$SERVICE_FILE" ]; then
        print_err "Не найден $SERVICE_FILE в $INSTALL_DIR"
        return 1
    fi

    cp "$INSTALL_DIR/$SERVICE_FILE" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" > /dev/null 2>&1

    print_ok "systemd сервис $SERVICE_NAME установлен и включён в автозапуск"
}

# Запуск сервиса
start_service() {
    systemctl start opengate
    sleep 2

    if systemctl is-active --quiet opengate; then
        print_ok "Бот запущен и работает!"
    else
        print_err "Бот не запустился. Проверьте логи:"
        echo "  systemctl status opengate"
        echo "  journalctl -u opengate -n 50"
    fi
}

# ============================================================
# ПУНКТ 1: УСТАНОВКА
# ============================================================
do_install() {
    print_header "🚀 Установка OpenGate"

    # Проверяем, не установлен ли уже
    if [ -d "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR/.git" ]; then
        print_warn "OpenGate уже установлен в $INSTALL_DIR"
        if [ "$AUTO_MODE" = "1" ]; then
            print_warn "Автоматический режим: принудительная переустановка"
            reinstall_choice="1"
        else
            echo ""
            echo "  1) Переустановить (удалить и установить заново)"
            echo "  2) Отмена"
            read -p "Выберите [1-2]: " reinstall_choice
        fi
        if [ "$reinstall_choice" != "1" ]; then
            echo "Установка отменена."
            return 0
        fi
        systemctl stop opengate 2>/dev/null || true
        # Сохраняем config.py и базу данных
        if [ -f "$INSTALL_DIR/config.py" ]; then
            cp "$INSTALL_DIR/config.py" /tmp/opengate_config_backup.py
            BACKUP_CONFIG=1
        fi
        if [ -f "$INSTALL_DIR/database/vpn_bot.db" ]; then
            cp "$INSTALL_DIR/database/vpn_bot.db" /tmp/opengate_db_backup.db
            BACKUP_DB=1
        fi
        safe_cd_away_from_install_dir
        rm -rf "$INSTALL_DIR"
    fi

    # Запрашиваем настройки до начала установки
    ask_config

    # Установка системных зависимостей
    install_system_deps
    ensure_system_user

    # Клонирование репозитория
    print_header "Загрузка OpenGate"
    safe_cd_away_from_install_dir
    if [ -d "$INSTALL_DIR" ]; then
        print_err "Каталог $INSTALL_DIR всё ещё существует — удалите вручную: rm -rf $INSTALL_DIR"
        return 1
    fi
    git clone "$REPO_URL" "$INSTALL_DIR" -q
    cd "$INSTALL_DIR" || { print_err "Не удалось перейти в $INSTALL_DIR"; return 1; }
    normalize_repo_files "$INSTALL_DIR"
    print_ok "Репозиторий клонирован"

    # Восстановление backup'ов при переустановке
    if [ "$BACKUP_CONFIG" = "1" ] && [ -f "/tmp/opengate_config_backup.py" ]; then
        cp /tmp/opengate_config_backup.py "$INSTALL_DIR/config.py"
        rm /tmp/opengate_config_backup.py
        print_ok "config.py восстановлен из резервной копии"
        NEED_WRITE_CONFIG=0
    fi
    if [ "$BACKUP_DB" = "1" ] && [ -f "/tmp/opengate_db_backup.db" ]; then
        mkdir -p "$INSTALL_DIR/database"
        cp /tmp/opengate_db_backup.db "$INSTALL_DIR/database/vpn_bot.db"
        rm /tmp/opengate_db_backup.db
        print_ok "База данных восстановлена из резервной копии"
    fi

    # Запись config.py
    write_config

    # Виртуальное окружение и зависимости
    setup_venv

    chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

    # Настройка автозапуска
    setup_systemd

    # Запуск
    print_header "Запуск бота"
    start_service

    print_header "✅ Установка завершена!"
    echo -e "  Директория: ${GREEN}$INSTALL_DIR${NC}"
    echo -e "  Виртуальное окружение: ${GREEN}$VENV_DIR${NC}"
    echo -e "  Управление сервисом:"
    echo -e "    ${CYAN}systemctl status opengate${NC}   — статус"
    echo -e "    ${CYAN}systemctl restart opengate${NC}  — перезапуск"
    echo -e "    ${CYAN}systemctl stop opengate${NC}     — остановка"
    echo -e "    ${CYAN}journalctl -u opengate -f${NC}   — логи"
}

# ============================================================
# ПУНКТ 2: МЯГКОЕ ОБНОВЛЕНИЕ (git pull)
# ============================================================
do_soft_update() {
    print_header "🔄 Мягкое обновление"

    if [ ! -d "$INSTALL_DIR/.git" ]; then
        print_err "OpenGate не установлен в $INSTALL_DIR"
        return 1
    fi

    cd "$INSTALL_DIR"

    # Сохраняем текущие изменения в stash (если есть)
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        print_warn "Обнаружены локальные изменения — сохраняем через git stash"
        git stash -q
        STASHED=1
    fi

    if [ -n "$TARGET_COMMIT" ]; then
        git fetch -q origin
        git checkout -q "$TARGET_COMMIT"
    else
        git checkout -q main
        git pull -q origin main
    fi

    if [ "$STASHED" = "1" ]; then
        git stash pop -q 2>/dev/null || print_warn "Не удалось восстановить локальные изменения (конфликт)"
    fi

    normalize_repo_files "$INSTALL_DIR"
    print_ok "Код обновлён"

    # Обновляем зависимости
    source "$VENV_DIR/bin/activate"
    pip install --upgrade -r requirements.txt -q
    deactivate
    print_ok "Зависимости обновлены"

    chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true

    # Перезапуск
    systemctl restart opengate
    sleep 2

    if systemctl is-active --quiet opengate; then
        print_ok "Бот перезапущен и работает!"
    else
        print_err "Бот не запустился после обновления"
        echo "  systemctl status opengate"
    fi
}

# ============================================================
# ПУНКТ 3: ЖЁСТКАЯ ПЕРЕЗАПИСЬ (git fetch + reset)
# ============================================================
do_hard_reset() {
    print_header "⚠️  Жёсткая перезапись"

    if [ ! -d "$INSTALL_DIR/.git" ]; then
        print_err "OpenGate не установлен в $INSTALL_DIR"
        return 1
    fi

    echo -e "${RED}Внимание! Все локальные изменения в коде будут перезаписаны.${NC}"
    echo -e "${YELLOW}config.py и database/vpn_bot.db затронуты НЕ будут.${NC}"
    if [ "$AUTO_MODE" = "1" ]; then
        confirm="y"
    else
        read -p "Продолжить? (y/N): " confirm
    fi
    if [[ ! "$confirm" =~ ^[YyДд]$ ]]; then
        echo "Отменено."
        return 0
    fi

    cd "$INSTALL_DIR"

    # Жёсткая перезапись: config.py и database/vpn_bot.db в .gitignore — не затрагиваются
    git fetch origin -q
    local target="origin/main"
    if [ -n "$TARGET_COMMIT" ]; then
        target="$TARGET_COMMIT"
    fi
    git reset --hard "$target" -q
    git clean -fd -q
    normalize_repo_files "$INSTALL_DIR"
    print_ok "Код перезаписан ($target)"

    # Обновляем зависимости
    source "$VENV_DIR/bin/activate"
    pip install --upgrade -r requirements.txt -q
    deactivate
    print_ok "Зависимости обновлены"

    chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true

    # Перезапуск
    systemctl restart opengate
    sleep 2

    if systemctl is-active --quiet opengate; then
        print_ok "Бот перезапущен и работает!"
    else
        print_err "Бот не запустился после перезаписи"
        echo "  systemctl status opengate"
    fi
}

# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================
show_menu() {
    clear
    echo -e "${CYAN}"
    echo "  ╔═══════════════════════════════════════╗"
    echo "  ║       🌐 OpenGate Manager         ║"
    echo "  ╚═══════════════════════════════════════╝"
    echo -e "${NC}"
    echo "  1) 🚀 Установка"
    echo "  2) 🔄 Мягкое обновление (git pull)"
    echo "  3) ⚠️  Жёсткая перезапись (с GitHub)"
    echo ""
    echo "  0) Выход"
    echo ""
    read -p "  Выберите действие [0-3]: " choice

    case $choice in
        1) do_install ;;
        2) do_soft_update ;;
        3) do_hard_reset ;;
        0) echo "Пока! 👋"; exit 0 ;;
        *) echo "Неверный выбор"; return 1 ;;
    esac
}

# Проверка root-прав
if [ "$EUID" -ne 0 ]; then
    print_err "Скрипт должен быть запущен от root (sudo)"
    exit 1
fi

# Проверка на автоматический режим (передан аргумент действия)
if [ -n "$1" ]; then
    ACTION="$1"
    export AUTO_MODE="1"
    
    case "$ACTION" in
        install)
            if [ -z "$2" ] || [ -z "$3" ]; then
                print_err "Для автоматической установки требуются BOT_TOKEN и ADMIN_ID"
                echo "Использование: bash install.sh install <BOT_TOKEN> <ADMIN_ID>"
                exit 1
            fi
            export BOT_TOKEN="$2"
            export ADMIN_ID="$3"
            do_install 
            ;;
        update)
            export TARGET_COMMIT="$2"
            do_soft_update 
            ;;
        reset)
            export TARGET_COMMIT="$2"
            do_hard_reset 
            ;;
        *)
            print_err "Неизвестное действие: $ACTION. Доступно: install, update, reset"
            exit 1
            ;;
    esac
    exit 0
fi

show_menu
