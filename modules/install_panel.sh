#!/usr/bin/env bash
# Установка Remnawave Panel + reverse proxy (Caddy) по официальной схеме:
# https://docs.rw/install/remnawave-panel + https://docs.rw/install/reverse-proxies/caddy
#
# Скрипт идемпотентен: если /opt/remnawave уже существует, спросит перед перезаписью.

install_panel() {
    log_step "Установка Remnawave Panel"

    ensure_docker

    local panel_dir="/opt/remnawave"
    if [[ -d "$panel_dir" && -f "$panel_dir/docker-compose.yml" ]]; then
        log_warn "Похоже, панель уже установлена в $panel_dir."
        confirm "Переустановить (текущие файлы будут перезаписаны, БД внутри Docker-тома не пострадает)?" "n" || return 0
    fi

    local panel_domain sub_domain use_bundled_sub
    panel_domain=$(ask "Домен панели (например panel.example.com), уже указывающий на этот сервер")

    confirm "Ставим также Subscription Page на этом же сервере (bundled)?" "y" && use_bundled_sub="y" || use_bundled_sub="n"

    if [[ "$use_bundled_sub" == "y" ]]; then
        sub_domain=$(ask "Домен для Subscription Page (например sub.example.com)")
    fi

    mkdir -p "$panel_dir" && cd "$panel_dir" || die "Не удалось создать $panel_dir"

    log_info "Скачиваю docker-compose.yml и .env.sample..."
    curl -fsSL -o docker-compose.yml \
        https://raw.githubusercontent.com/remnawave/backend/refs/heads/main/docker-compose-prod.yml \
        || die "Не удалось скачать docker-compose.yml"
    curl -fsSL -o .env \
        https://raw.githubusercontent.com/remnawave/backend/refs/heads/main/.env.sample \
        || die "Не удалось скачать .env.sample"

    log_info "Генерирую секретные ключи..."
    sed -i "s/^APP_SECRET=.*/APP_SECRET=$(openssl rand -hex 64)/" .env
    sed -i "s/^METRICS_PASS=.*/METRICS_PASS=$(openssl rand -hex 64)/" .env
    sed -i "s/^WEBHOOK_SECRET_HEADER=.*/WEBHOOK_SECRET_HEADER=$(openssl rand -hex 64)/" .env

    local pg_pw
    pg_pw=$(openssl rand -hex 24)
    sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$pg_pw/" .env
    sed -i "s|^\(DATABASE_URL=\"postgresql://postgres:\)[^\@]*\(@.*\)|\1$pg_pw\2|" .env

    sed -i "s/^FRONT_END_DOMAIN=.*/FRONT_END_DOMAIN=$panel_domain/" .env

    if [[ "$use_bundled_sub" == "y" ]]; then
        sed -i "s/^SUB_PUBLIC_DOMAIN=.*/SUB_PUBLIC_DOMAIN=$sub_domain/" .env
    else
        sed -i "s/^SUB_PUBLIC_DOMAIN=.*/SUB_PUBLIC_DOMAIN=$panel_domain\/api\/sub/" .env
    fi

    log_step "Запускаю контейнеры панели"
    docker compose up -d || die "Не удалось запустить контейнеры панели"

    log_ok "Панель поднята. Проверить логи: cd $panel_dir && docker compose logs -f -t"

    install_reverse_proxy_caddy "$panel_domain" "$use_bundled_sub" "${sub_domain:-}"

    if [[ "$use_bundled_sub" == "y" ]]; then
        FASTVPN_SUB_DOMAIN="$sub_domain"
        install_subscription_page "$sub_domain"
    fi

    echo
    log_ok "Готово! Откройте https://$panel_domain в браузере и завершите первичную настройку (создание админа)."
    log_info "Домен панели:      https://$panel_domain"
    [[ "$use_bundled_sub" == "y" ]] && log_info "Domain sub-страницы: https://$sub_domain"
    pause
}

# install_reverse_proxy_caddy <panel_domain> <use_sub y|n> <sub_domain>
install_reverse_proxy_caddy() {
    local panel_domain="$1" use_sub="$2" sub_domain="${3:-}"

    log_step "Настройка Caddy (reverse proxy)"

    local caddy_dir="/opt/remnawave/caddy"
    mkdir -p "$caddy_dir" && cd "$caddy_dir" || die "Не удалось создать $caddy_dir"

    {
        echo "https://${panel_domain} {"
        echo "        encode"
        echo "        reverse_proxy * http://remnawave:3000"
        echo "}"
        if [[ "$use_sub" == "y" && -n "$sub_domain" ]]; then
            echo "https://${sub_domain} {"
            echo "        encode"
            echo "        reverse_proxy * http://remnawave-subscription-page:3010"
            echo "}"
        fi
        echo ":443 {"
        echo "    tls internal"
        echo "    respond 204"
        echo "}"
    } > Caddyfile

    cat > docker-compose.yml <<'EOF'
services:
  caddy:
    image: caddy:2.9
    container_name: 'caddy'
    hostname: caddy
    restart: always
    ports:
      - '0.0.0.0:443:443'
      - '0.0.0.0:80:80'
    networks:
      - remnawave-network
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy-ssl-data:/data

networks:
  remnawave-network:
    name: remnawave-network
    driver: bridge
    external: true

volumes:
  caddy-ssl-data:
    driver: local
    external: false
    name: caddy-ssl-data
EOF

    docker compose up -d || die "Не удалось запустить Caddy"
    log_ok "Caddy запущен, SSL-сертификаты будут выпущены автоматически при первом обращении к домену."
}

# install_subscription_page <sub_domain>  (bundled-режим, https://docs.rw/install/subscription-page/bundled)
install_subscription_page() {
    local sub_domain="$1"

    log_step "Установка Subscription Page (bundled)"

    log_warn "Перед продолжением создайте API-токен в панели: Settings -> API Tokens."
    local api_token
    api_token=$(ask_secret "Вставьте API-токен панели")

    local sub_dir="/opt/remnawave/subscription"
    mkdir -p "$sub_dir" && cd "$sub_dir" || die "Не удалось создать $sub_dir"

    cat > docker-compose.yml <<'EOF'
services:
  remnawave-subscription-page:
    image: remnawave/subscription-page:latest
    container_name: remnawave-subscription-page
    hostname: remnawave-subscription-page
    restart: always
    env_file:
      - .env
    ports:
      - '127.0.0.1:3010:3010'
    networks:
      - remnawave-network

networks:
  remnawave-network:
    driver: bridge
    external: true
EOF

    cat > .env <<EOF
APP_PORT=3010
REMNAWAVE_PANEL_URL=http://remnawave:3000
REMNAWAVE_API_TOKEN=${api_token}
TRUST_PROXY=1
EOF

    docker compose up -d || die "Не удалось запустить Subscription Page"
    log_ok "Subscription Page запущена: https://${sub_domain}/<shortUuid пользователя>"
    log_info "Кастомизация (лого, тексты, поддерживаемые приложения) — в панели: Subpage Builder."
}
