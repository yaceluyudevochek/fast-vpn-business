#!/usr/bin/env bash
# Подключение этого сервера как Remnawave-ноды.
# Полуавтоматический процесс — так же, как рекомендует сама Remnawave
# (https://docs.rw/install/remnawave-node): нода регистрируется через UI
# панели (там генерируются уникальные NODE_PORT и SECRET_KEY), а этот
# скрипт разворачивает готовый docker-compose.yml на сервере ноды.

install_node() {
    log_step "Подключение ноды"

    ensure_docker

    echo -e "${C_DIM}Сначала нужно зарегистрировать ноду в панели:${C_RESET}"
    echo "  1. Откройте панель -> Nodes -> Management -> кнопка '+'."
    echo "  2. Укажите имя ноды и IP-адрес ЭТОГО сервера ($( (curl -fsS -4 ifconfig.me 2>/dev/null) || echo 'не удалось определить')).' "
    echo "  3. Нажмите на карточке ноды кнопку 'Copy docker-compose.yml'."
    echo -e "  4. ${C_BOLD}Пока не нажимайте Create${C_RESET} — сначала разверните конфиг здесь."
    echo
    confirm "Docker-compose.yml из панели скопирован в буфер обмена?" "y" || { log_warn "Ок, вернитесь сюда, когда будет готово."; return 0; }

    local node_dir="/opt/remnanode"
    mkdir -p "$node_dir" && cd "$node_dir" || die "Не удалось создать $node_dir"

    echo
    log_info "Вставьте содержимое docker-compose.yml из панели, затем на новой строке введите ${C_BOLD}EOF${C_RESET} и Enter:"
    local compose_content="" line
    while IFS= read -r line; do
        [[ "$line" == "EOF" ]] && break
        compose_content+="$line"$'\n'
    done
    [[ -z "$compose_content" ]] && die "Пустой docker-compose.yml, отмена."

    printf '%s' "$compose_content" > docker-compose.yml
    log_ok "Файл сохранён: $node_dir/docker-compose.yml"

    local node_port
    node_port=$(grep -oP 'NODE_PORT=\K[0-9]+' docker-compose.yml | head -1)

    log_step "Запускаю контейнер ноды"
    docker compose up -d || die "Не удалось запустить контейнер ноды"
    log_ok "Контейнер remnanode запущен."

    if command -v ufw >/dev/null 2>&1 && [[ -n "$node_port" ]]; then
        confirm "Ограничить порт $node_port файрволом только для IP панели (рекомендуется)?" "y" && {
            local panel_ip
            panel_ip=$(ask "IP-адрес сервера панели")
            ufw allow from "$panel_ip" to any port "$node_port" proto tcp comment "remnanode from panel"
            ufw deny "$node_port"/tcp comment "remnanode deny others" || true
            log_ok "Порт $node_port открыт только для $panel_ip."
        }
    else
        log_warn "ufw не найден или NODE_PORT не определён — настройте файрвол вручную (порт $node_port только для IP панели)."
    fi

    echo
    log_ok "Готово с этой стороны. Вернитесь в панель и завершите добавление ноды:"
    echo "  Next -> выберите Config Profile -> Create."
    pause
}
