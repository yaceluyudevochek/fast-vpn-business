#!/usr/bin/env bash
# Полная очистка того, что поставил Fast VPN Business на ЭТОМ сервере:
# панель + Caddy + Subscription Page, нода, бот. Каждый шаг необратим
# (удаляет данные Docker-томов и файлы на диске), поэтому перед реальным
# удалением требуется явное подтверждение вводом слова УДАЛИТЬ.

# confirm_destructive "Что удаляем" -> 0 если пользователь ввёл ровно "УДАЛИТЬ"
confirm_destructive() {
    local what="$1" answer
    echo
    log_warn "Это НЕОБРАТИМО удалит: ${what}"
    read -r -p "$(echo -e "${C_BOLD}Введите УДАЛИТЬ (заглавными), чтобы подтвердить${C_RESET}: ")" answer
    [[ "$answer" == "УДАЛИТЬ" ]]
}

# stop_and_remove_compose <каталог>
_compose_down_v() {
    local dir="$1"
    if [[ -f "$dir/docker-compose.yml" ]]; then
        (cd "$dir" && docker compose down -v --remove-orphans) 2>&1 | sed 's/^/    /'
    fi
}

cleanup_panel() {
    local panel_dir="/opt/remnawave"
    if [[ ! -d "$panel_dir" ]]; then
        log_info "Панель не найдена в $panel_dir — пропускаю."
        return 0
    fi

    confirm_destructive "панель Remnawave, ВСЮ базу данных (пользователи, ноды, подписки), Caddy, Subscription Page и все файлы в $panel_dir" \
        || { log_info "Отменено."; return 0; }

    log_step "Останавливаю и удаляю контейнеры панели"
    _compose_down_v "$panel_dir"
    _compose_down_v "$panel_dir/caddy"
    _compose_down_v "$panel_dir/subscription"

    docker network rm remnawave-network >/dev/null 2>&1 || true

    rm -rf "$panel_dir"
    log_ok "Панель, Caddy и Subscription Page удалены."
}

cleanup_node() {
    local node_dir="/opt/remnanode"
    if [[ ! -d "$node_dir" ]]; then
        log_info "Нода не найдена в $node_dir — пропускаю."
        return 0
    fi

    confirm_destructive "контейнер ноды и все файлы в $node_dir" \
        || { log_info "Отменено."; return 0; }

    log_step "Останавливаю и удаляю контейнер ноды"
    _compose_down_v "$node_dir"
    rm -rf "$node_dir"

    if command -v ufw >/dev/null 2>&1; then
        log_warn "Если ранее добавлялось правило ufw под порт ноды — удалите его вручную: ufw status numbered"
    fi

    log_ok "Нода удалена."
}

cleanup_bot() {
    local bot_dir="${1:-/opt/fastvpnbot}"

    local has_service=0
    systemctl list-unit-files 2>/dev/null | grep -q "^fastvpnbot.service" && has_service=1

    if [[ "$has_service" -eq 0 && ! -d "$bot_dir" ]]; then
        log_info "Бот не найден (нет ни сервиса fastvpnbot, ни каталога $bot_dir) — пропускаю."
        return 0
    fi

    confirm_destructive "systemd-сервис fastvpnbot и все файлы бота в $bot_dir (включая базу данных подписок и config.py)" \
        || { log_info "Отменено."; return 0; }

    if [[ -f "$bot_dir/config/config.py" || -f "$bot_dir/bot_database.db" ]]; then
        local backup_dir="/root/fastvpn_bot_backup_$(date +%Y%m%d_%H%M%S)"
        if confirm "Сохранить резервную копию config.py и базы данных в $backup_dir перед удалением?" "y"; then
            mkdir -p "$backup_dir"
            [[ -f "$bot_dir/config/config.py" ]] && cp "$bot_dir/config/config.py" "$backup_dir/" 2>/dev/null
            cp "$bot_dir"/*.db "$backup_dir/" 2>/dev/null
            log_ok "Бэкап сохранён в $backup_dir"
        fi
    fi

    if [[ "$has_service" -eq 1 ]]; then
        log_step "Останавливаю сервис fastvpnbot"
        systemctl stop fastvpnbot 2>/dev/null || true
        systemctl disable fastvpnbot 2>/dev/null || true
        rm -f /etc/systemd/system/fastvpnbot.service
        systemctl daemon-reload
    fi

    rm -rf "$bot_dir"
    log_ok "Бот удалён."
}

# Полная очистка: спрашивает по каждому компоненту, что нашла на этом сервере.
cleanup_all() {
    log_step "Полная очистка Fast VPN Business на этом сервере"

    local found_panel=0 found_node=0 found_bot=0
    [[ -d /opt/remnawave ]] && found_panel=1
    [[ -d /opt/remnanode ]] && found_node=1
    { systemctl list-unit-files 2>/dev/null | grep -q "^fastvpnbot.service"; } || [[ -d /opt/fastvpnbot ]] && found_bot=1

    if [[ "$found_panel" -eq 0 && "$found_node" -eq 0 && "$found_bot" -eq 0 ]]; then
        log_ok "На этом сервере не найдено ни панели, ни ноды, ни бота Fast VPN Business — нечего удалять."
        pause
        return 0
    fi

    echo "Найдено на этом сервере:"
    [[ "$found_panel" -eq 1 ]] && echo "  - Remnawave Panel / Caddy / Subscription Page (/opt/remnawave)"
    [[ "$found_node" -eq 1 ]]  && echo "  - Remnawave Node (/opt/remnanode)"
    [[ "$found_bot" -eq 1 ]]   && echo "  - Telegram-бот (systemd fastvpnbot)"
    echo

    if ! confirm "Продолжить очистку ВСЕГО перечисленного?" "n"; then
        log_info "Отменено."
        return 0
    fi

    [[ "$found_panel" -eq 1 ]] && cleanup_panel
    [[ "$found_node" -eq 1 ]]  && cleanup_node
    [[ "$found_bot" -eq 1 ]]   && cleanup_bot

    if confirm "Также удалить сам Docker (docker-ce и все прочие контейнеры/образы на сервере)?" "n"; then
        confirm_destructive "Docker Engine и ВСЕ Docker-данные на сервере (в т.ч. не относящиеся к Fast VPN Business)" && {
            systemctl stop docker 2>/dev/null || true
            apt-get purge -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1 || true
            rm -rf /var/lib/docker /var/lib/containerd
            log_ok "Docker удалён."
        }
    fi

    echo
    log_ok "Очистка завершена."
    pause
}

# Подменю очистки — вызывается из главного меню install.sh.
cleanup_menu() {
    while true; do
        print_banner
        echo -e "${C_BOLD}Очистка — выберите, что удалить:${C_RESET}"
        echo "  1) Только панель + Caddy + Subscription Page"
        echo "  2) Только ноду на этом сервере"
        echo "  3) Только Telegram-бота"
        echo "  4) Всё, что найдено на этом сервере (панель/нода/бот)"
        echo "  5) Назад"
        echo
        local choice
        read -r -p "$(echo -e "${C_BOLD}>${C_RESET} ")" choice
        case "$choice" in
            1) cleanup_panel; pause ;;
            2) cleanup_node; pause ;;
            3) cleanup_bot; pause ;;
            4) cleanup_all ;;
            5) return 0 ;;
            *) log_warn "Некорректный выбор."; sleep 1 ;;
        esac
    done
}
