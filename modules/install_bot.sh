#!/usr/bin/env bash
# Установка Telegram-бота (white-label, на основе Remnawave API + Platega.io).
# Копирует ./bot из репозитория Fast VPN Business, разворачивает venv,
# запускает мастер настройки конфига и поднимает systemd-сервис.

install_bot() {
    log_step "Установка Telegram-бота"

    local bot_dir
    bot_dir=$(ask "Каталог установки бота" "/opt/fastvpnbot")

    if [[ -d "$bot_dir" && -f "$bot_dir/config/config.py" ]]; then
        log_warn "В $bot_dir уже есть настроенный бот."
        confirm "Переустановить код бота (config.py и база данных сохранятся)?" "n" || return 0
    fi

    log_step "Устанавливаю системные зависимости (python3, venv, pip)"
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip >/dev/null \
        || die "Не удалось установить Python."

    mkdir -p "$bot_dir"
    log_info "Копирую код бота в $bot_dir..."
    # Исходное дерево ./bot не содержит ни config.py, ни *.db — только .example
    # и код, поэтому обычное копирование поверх безопасно для повторных запусков:
    # существующие config.py / база данных / venv адресата никогда не задеваются.
    cp -r "$SCRIPT_DIR/bot/." "$bot_dir/"

    cd "$bot_dir" || die "Не удалось перейти в $bot_dir"

    if [[ ! -d venv ]]; then
        log_info "Создаю виртуальное окружение..."
        python3 -m venv venv || die "Не удалось создать venv"
    fi
    log_info "Устанавливаю зависимости бота (aiogram, aiohttp, aiosqlite)..."
    venv/bin/pip install --upgrade pip -q
    venv/bin/pip install -q -r requirements || die "Не удалось установить зависимости"

    if [[ ! -f config/config.py ]]; then
        run_white_label_wizard "$bot_dir"
    else
        log_ok "config/config.py уже существует — не трогаю (запускаю бота с текущими настройками)."
    fi

    setup_bot_systemd_service "$bot_dir"

    log_ok "Бот установлен и запущен. Логи: journalctl -u fastvpnbot -f"
    pause
}

# Мастер первичной настройки white-label бота: собирает config/config.py
# и (опционально) картинки экранов.
run_white_label_wizard() {
    local bot_dir="$1"

    log_step "Мастер настройки бота (white-label)"

    local token panel_url sub_domain admin_id panel_key
    local platega_merchant platega_secret platega_webhook_url success_url failed_url
    local agreement_url privacy_url support_url

    token=$(ask_secret "Токен Telegram-бота (от @BotFather)")
    admin_id=$(ask "Telegram ID администратора (числовой)")
    panel_url=$(ask "URL панели Remnawave (например https://panel.example.com)")
    panel_key=$(ask_secret "API-ключ панели Remnawave (Settings -> API Tokens)")
    sub_domain=$(ask "Публичный домен подписок (SUB_PUBLIC_DOMAIN из панели)" "$panel_url")

    echo
    log_info "Платежи через Platega.io (личный кабинет -> реквизиты API):"
    platega_merchant=$(ask "Platega Merchant ID")
    platega_secret=$(ask_secret "Platega Secret Key")

    local bot_username
    bot_username=$(ask "Username вашего бота без @ (для ссылок после оплаты)")
    success_url="https://t.me/${bot_username}"
    failed_url="https://t.me/${bot_username}"

    local webhook_domain
    webhook_domain=$(ask "Домен/IP, на который Platega будет слать callback (обычно домен панели)" "$panel_url")
    platega_webhook_url="${webhook_domain%/}/webhook/platega"

    echo
    log_info "Публичные ссылки бота (можно поменять позже в config.py):"
    agreement_url=$(ask "Ссылка на пользовательское соглашение" "https://telegra.ph/your-agreement")
    privacy_url=$(ask "Ссылка на политику конфиденциальности" "https://telegra.ph/your-privacy-policy")
    support_url=$(ask "Ссылка на поддержку (t.me/...)" "https://t.me/your_support")

    local webhook_port
    webhook_port=$(ask "Порт для приёма callback'ов Platega (должен быть свободен и проброшен реверс-прокси)" "8080")

    cat > "$bot_dir/config/config.py" <<EOF
TOKEN = "${token}"

PANEL_API_URL = "${panel_url%/}"
SUB_DOMAIN_URL = "${sub_domain%/}"

ADMIN_ID = ${admin_id}

PANEL_API_KEY = "${panel_key}"

# ======================================================
# Platega.io (Платежная система)
# ======================================================

PLATEGA_MERCHANT_ID = "${platega_merchant}"
PLATEGA_SECRET_KEY = "${platega_secret}"

PLATEGA_API_URL = "https://app.platega.io"

PLATEGA_WEBHOOK_URL = "${platega_webhook_url}"

PLATEGA_SUCCESS_URL = "${success_url}"
PLATEGA_FAILED_URL = "${failed_url}"

WEBHOOK_HOST = "0.0.0.0"
WEBHOOK_PORT = ${webhook_port}

AGREEMENT_URL = "${agreement_url}"
PRIVACY_URL = "${privacy_url}"
SUPPORT_URL = "${support_url}"
EOF
    chmod 600 "$bot_dir/config/config.py"
    log_ok "config/config.py создан."

    echo
    if confirm "Добавить свои картинки для экранов бота сейчас?" "n"; then
        log_info "Разместите 7 файлов (см. $bot_dir/assets/images/README.md) в $bot_dir/assets/images/ и запустите бота заново."
        log_info "Можно сделать это и позже — без картинок бот работает как обычные текстовые сообщения."
    fi
}

setup_bot_systemd_service() {
    local bot_dir="$1"

    log_step "Настраиваю systemd-сервис fastvpnbot"

    cat > /etc/systemd/system/fastvpnbot.service <<EOF
[Unit]
Description=Fast VPN Business Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${bot_dir}
ExecStart=${bot_dir}/venv/bin/python ${bot_dir}/main.py
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable fastvpnbot >/dev/null 2>&1
    systemctl restart fastvpnbot
    sleep 2
    if systemctl is-active --quiet fastvpnbot; then
        log_ok "Сервис fastvpnbot запущен."
    else
        log_err "Сервис не запустился, смотрите: journalctl -u fastvpnbot -n 50"
    fi
}
