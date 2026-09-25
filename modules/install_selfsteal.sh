#!/usr/bin/env bash
# Caddy Selfsteal — веб-сервер маскировки Reality-трафика: отдаёт настоящий
# HTTPS-сайт с реальным сертификатом на том порту, который Xray Reality
# использует как "dest" (fallback для всех, кто ломится не через Xray-клиент,
# включая active-probing со стороны цензора). Устанавливается на том же
# сервере, что и Remnawave Node (dest слушает 127.0.0.1).
#
# Логика и шаблоны сайтов адаптированы из MIT-проекта Case211/remnanode-install
# (https://github.com/Case211/remnanode-install, раздел Caddy Selfsteal) —
# см. caddy-selfsteal/templates/SOURCE.md.

CADDY_DIR="/opt/caddy"
CADDY_HTML_DIR="$CADDY_DIR/html"
CADDY_VERSION="2.10.2"
DEFAULT_SELFSTEAL_PORT="9443"

# folder:описание — соответствует caddy-selfsteal/templates/<folder>
SELFSTEAL_TEMPLATES=(
    "10gag:Сайт мемов (10gag)"
    "503-1:Страница ошибки 503 (v1)"
    "503-2:Страница ошибки 503 (v2)"
    "convertit:Конвертер файлов (Convertit)"
    "converter:Видеостудия-конвертер"
    "downloader:Даунлоадер"
    "filecloud:Облачное хранилище"
    "games-site:Ретро игровой портал"
    "modmanager:Мод-менеджер для игр"
    "speedtest:Спидтест"
    "YouTube:Видеохостинг с капчей"
)

_selfsteal_server_ip() {
    local ip
    ip=$(curl -s -4 --connect-timeout 5 ifconfig.io 2>/dev/null | tr -d '[:space:]') \
        || ip=$(curl -s -4 --connect-timeout 5 icanhazip.com 2>/dev/null | tr -d '[:space:]') \
        || ip="127.0.0.1"
    echo "${ip:-127.0.0.1}"
}

_selfsteal_validate_cloudflare_token() {
    local token="$1" response
    response=$(curl -s --connect-timeout 10 --max-time 15 \
        -H "Authorization: Bearer $token" \
        "https://api.cloudflare.com/client/v4/user/tokens/verify" 2>/dev/null) || true

    if echo "$response" | grep -q '"success":true'; then
        return 0
    fi
    local error_msg
    error_msg=$(echo "$response" | sed -n 's/.*"message":"\([^"]*\)".*/\1/p' | head -1)
    log_err "Cloudflare API Token невалиден${error_msg:+: $error_msg}"
    return 1
}

_selfsteal_check_dns() {
    local domain="$1" server_ip="$2" dns_ip
    if ! command -v dig >/dev/null 2>&1; then
        apt-get install -y -qq dnsutils >/dev/null 2>&1 || true
    fi
    dns_ip=$(dig +short "$domain" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | tail -1)
    if [[ -z "$dns_ip" ]]; then
        log_warn "Не удалось получить A-запись для $domain."
        return 1
    fi
    if [[ "$dns_ip" != "$server_ip" ]]; then
        log_warn "DNS не совпадает: $domain -> $dns_ip, а IP сервера — $server_ip."
        return 1
    fi
    log_ok "DNS настроен верно: $domain -> $dns_ip"
    return 0
}

# Копирует локально вендоренный шаблон в $CADDY_HTML_DIR
_selfsteal_install_template() {
    local folder="$1"
    local src="$SCRIPT_DIR/caddy-selfsteal/templates/$folder"

    mkdir -p "$CADDY_HTML_DIR"
    find "$CADDY_HTML_DIR" -mindepth 1 -delete 2>/dev/null || true

    if [[ -d "$src" ]]; then
        cp -r "$src"/. "$CADDY_HTML_DIR/"
        rm -f "$CADDY_HTML_DIR/SOURCE.md"
        log_ok "Шаблон '$folder' установлен."
    else
        log_warn "Шаблон '$folder' не найден локально, создаю простую заглушку."
        cat > "$CADDY_HTML_DIR/index.html" <<'EOF'
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Welcome</title></head>
<body><h1>Welcome</h1></body></html>
EOF
    fi
}

_selfsteal_select_template() {
    echo -e "${C_BOLD}Выберите шаблон маскировки:${C_RESET}"
    local i=1
    for entry in "${SELFSTEAL_TEMPLATES[@]}"; do
        printf "  %2d) %s\n" "$i" "${entry#*:}"
        i=$((i + 1))
    done
    printf "  %2d) ${C_YELLOW}Случайный${C_RESET}\n" "$i"
    echo
    local choice
    read -r -p "$(echo -e "${C_BOLD}>${C_RESET} ")" choice
    choice="${choice:-$i}"
    if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > i )); then
        log_warn "Некорректный выбор, беру случайный шаблон."
        choice="$i"
    fi

    local folder
    if (( choice == i )); then
        local random_entry="${SELFSTEAL_TEMPLATES[$RANDOM % ${#SELFSTEAL_TEMPLATES[@]}]}"
        folder="${random_entry%%:*}"
        log_info "Случайный выбор: ${random_entry#*:}"
    else
        local idx=$((choice - 1))
        folder="${SELFSTEAL_TEMPLATES[$idx]%%:*}"
    fi
    _selfsteal_install_template "$folder"
}

check_existing_selfsteal() {
    [[ -f "$CADDY_DIR/docker-compose.yml" ]]
}

install_selfsteal() {
    log_step "Установка Caddy Selfsteal (маскировка Reality)"

    ensure_docker

    if check_existing_selfsteal; then
        log_warn "Caddy Selfsteal уже установлен в $CADDY_DIR."
        if confirm "Переустановить (текущий Caddyfile/.env будут перезаписаны)?" "n"; then
            (cd "$CADDY_DIR" && docker compose down 2>/dev/null) || true
        else
            log_info "Оставляю как есть. Сменить только шаблон можно пунктом меню 'Сменить шаблон'."
            return 0
        fi
    fi

    mkdir -p "$CADDY_DIR" "$CADDY_HTML_DIR" "$CADDY_DIR/logs"

    echo
    log_info "Домен должен совпадать с serverNames в Xray Reality inbound'е этой ноды."
    local original_domain
    while true; do
        original_domain=$(ask "Домен для Selfsteal (например reality.example.com)")
        [[ "$original_domain" =~ [[:space:]] ]] && { log_warn "Домен не должен содержать пробелов."; continue; }
        [[ "$original_domain" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] && [[ "$original_domain" == *.* ]] && break
        log_warn "Неверный формат домена."
    done

    echo
    echo "Тип SSL-сертификата:"
    echo "  1) Обычный (HTTP-01 challenge)"
    echo "  2) Wildcard (DNS-01 через Cloudflare)"
    local cert_choice
    cert_choice=$(ask "Выбор [1-2]" "1")

    local domain="$original_domain" root_domain="" use_wildcard="n" cf_token=""
    local caddy_image="caddy:${CADDY_VERSION}"

    if [[ "$cert_choice" == "2" ]]; then
        use_wildcard="y"
        caddy_image="caddybuilds/caddy-cloudflare:latest"
        echo
        log_info "Cloudflare Dashboard -> My Profile -> API Tokens -> создайте токен с правами Zone:Zone:Read и Zone:DNS:Edit для нужной зоны."
        cf_token=$(ask_secret "Cloudflare API Token")
        cf_token=$(echo "$cf_token" | tr -d '\r\n ' | sed 's/[^a-zA-Z0-9_-]//g')

        if ! _selfsteal_validate_cloudflare_token "$cf_token"; then
            confirm "Токен не прошёл проверку. Продолжить всё равно?" "n" || { log_warn "Установка отменена."; return 1; }
        else
            log_ok "Cloudflare API Token валиден."
        fi

        root_domain=$(echo "$original_domain" | sed 's/^[^.]*\.//')
        if [[ "$root_domain" != "$original_domain" && -n "$root_domain" ]]; then
            domain="*.$root_domain"
        else
            root_domain="$original_domain"
            domain="*.$original_domain"
        fi
        log_info "Wildcard-домен для сертификата: $domain (в Xray serverNames используйте: $original_domain)"
    fi

    echo
    local server_ip
    server_ip=$(_selfsteal_server_ip)
    if confirm "Проверить, что DNS домена указывает на этот сервер ($server_ip)?" "y"; then
        _selfsteal_check_dns "$original_domain" "$server_ip" || confirm "Продолжить установку несмотря на DNS?" "y" || return 1
    fi

    local port
    port=$(ask "HTTPS-порт для Selfsteal (это же значение — 'dest' в Xray Reality)" "$DEFAULT_SELFSTEAL_PORT")
    while ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); do
        log_warn "Некорректный порт."
        port=$(ask "HTTPS-порт для Selfsteal" "$DEFAULT_SELFSTEAL_PORT")
    done

    cat > "$CADDY_DIR/.env" <<EOF
# Caddy Selfsteal Configuration
SELF_STEAL_DOMAIN=${domain}
SELF_STEAL_PORT=${port}
# Generated on $(date)
# Server IP: ${server_ip}
EOF
    if [[ "$use_wildcard" == "y" ]]; then
        {
            echo "CLOUDFLARE_API_TOKEN=${cf_token}"
            echo "# Original domain for Xray serverNames: ${original_domain}"
        } >> "$CADDY_DIR/.env"
    fi
    chmod 600 "$CADDY_DIR/.env"

    cat > "$CADDY_DIR/docker-compose.yml" <<EOF
services:
  caddy:
    image: ${caddy_image}
    container_name: caddy-selfsteal
    restart: unless-stopped
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - ${CADDY_HTML_DIR}:/var/www/html
      - ./logs:/var/log/caddy
      - caddy_data:/data
      - caddy_config:/config
    env_file:
      - .env
    network_mode: "host"
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  caddy_data:
    name: caddy_data
  caddy_config:
    name: caddy_config
EOF

    if [[ "$use_wildcard" == "y" ]]; then
        cat > "$CADDY_DIR/Caddyfile" <<'EOF'
{
	https_port {$SELF_STEAL_PORT}
	default_bind 127.0.0.1
	auto_https disable_redirects
	log {
		output file /var/log/caddy/default.log {
			roll_size 10MB
			roll_keep 5
			roll_keep_for 720h
		}
		level INFO
		format json
	}
}

:80 {
	bind 0.0.0.0
	redir https://{host}{uri} permanent
	log {
		output file /var/log/caddy/redirect.log {
			roll_size 5MB
			roll_keep 3
			roll_keep_for 168h
		}
	}
}

https://{$SELF_STEAL_DOMAIN} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	root * /var/www/html
	try_files {path} /index.html
	file_server
	log {
		output file /var/log/caddy/access.log {
			roll_size 10MB
			roll_keep 5
			roll_keep_for 720h
		}
		level INFO
		format json
	}
}
EOF
    else
        cat > "$CADDY_DIR/Caddyfile" <<'EOF'
{
	https_port {$SELF_STEAL_PORT}
	default_bind 127.0.0.1
	auto_https disable_redirects
	log {
		output file /var/log/caddy/default.log {
			roll_size 10MB
			roll_keep 5
			roll_keep_for 720h
		}
		level INFO
		format json
	}
}

http://{$SELF_STEAL_DOMAIN} {
	bind 0.0.0.0
	redir https://{host}{uri} permanent
	log {
		output file /var/log/caddy/redirect.log {
			roll_size 5MB
			roll_keep 3
			roll_keep_for 168h
		}
	}
}

https://{$SELF_STEAL_DOMAIN} {
	tls {
		issuer acme {
			disable_tlsalpn_challenge
		}
	}
	root * /var/www/html
	try_files {path} /index.html
	file_server
	log {
		output file /var/log/caddy/access.log {
			roll_size 10MB
			roll_keep 5
			roll_keep_for 720h
		}
		level INFO
		format json
	}
}

:80 {
	bind 0.0.0.0
	respond 204
	log off
}
EOF
    fi
    log_ok "Caddyfile и docker-compose.yml созданы."

    echo
    _selfsteal_select_template

    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        log_warn "Порт ${port} уже занят другим процессом."
        confirm "Всё равно запустить Caddy?" "n" || { log_info "Запуск отложен: cd $CADDY_DIR && docker compose up -d"; return 0; }
    fi

    log_step "Запускаю Caddy Selfsteal"
    (cd "$CADDY_DIR" && docker compose pull --quiet 2>/dev/null; docker compose up -d) \
        || die "Не удалось запустить Caddy Selfsteal"

    sleep 3
    if (cd "$CADDY_DIR" && docker compose ps caddy 2>/dev/null | grep -qE "Up|running"); then
        log_ok "Caddy Selfsteal запущен."
    else
        log_warn "Проверьте логи: cd $CADDY_DIR && docker compose logs"
    fi

    echo
    log_ok "Готово. Настройки для Xray Reality inbound'а на этой ноде:"
    if [[ "$use_wildcard" == "y" ]]; then
        echo "  serverNames: [\"${original_domain}\", \"${root_domain}\"]"
        log_info "Wildcard-сертификат покрывает все поддомены *.${root_domain}"
    else
        echo "  serverNames: [\"${original_domain}\"]"
    fi
    echo "  dest: \"127.0.0.1:${port}\""
    echo "  xver: 0"
    pause
}

change_selfsteal_template() {
    if ! check_existing_selfsteal; then
        log_err "Caddy Selfsteal не установлен (нет $CADDY_DIR/docker-compose.yml)."
        pause
        return 1
    fi
    log_step "Смена шаблона Caddy Selfsteal"
    _selfsteal_select_template
    log_ok "Шаблон обновлён. Перезапуск Caddy не требуется — файлы отдаются напрямую."
    pause
}

selfsteal_menu() {
    while true; do
        print_banner
        echo -e "${C_BOLD}Caddy Selfsteal (маскировка Reality):${C_RESET}"
        echo "  1) Установить / переустановить"
        echo "  2) Сменить HTML-шаблон"
        echo "  3) Назад"
        echo
        local choice
        read -r -p "$(echo -e "${C_BOLD}>${C_RESET} ")" choice
        case "$choice" in
            1) install_selfsteal ;;
            2) change_selfsteal_template ;;
            3) return 0 ;;
            *) log_warn "Некорректный выбор."; sleep 1 ;;
        esac
    done
}
