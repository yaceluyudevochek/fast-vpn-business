#!/usr/bin/env bash
#
# Fast VPN Business — универсальный установщик VPN-бизнеса под ключ:
# Remnawave Panel + Subscription Page + Nodes + Telegram-бот (white-label).
#
# Первый запуск:
#   curl -fsSL https://raw.githubusercontent.com/yaceluyudevochek/fast-vpn-business/main/install.sh -o install.sh
#   sudo bash install.sh
#
# Повторный запуск (репозиторий уже склонирован в /opt/fast-vpn-business):
#   sudo bash /opt/fast-vpn-business/install.sh
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Если скрипт запущен через curl | bash (нет modules/ рядом) — клонируем
# репозиторий в постоянный каталог /opt/fast-vpn-business (а не во
# временный, чтобы меню можно было открыть повторно без повторного
# скачивания) и переисполняем оттуда. Если каталог уже существует —
# обновляем его через git pull.
# ---------------------------------------------------------------------------
if [[ ! -d "$SCRIPT_DIR/modules" ]]; then
    REPO_URL="${FASTVPN_REPO_URL:-https://github.com/yaceluyudevochek/fast-vpn-business}"
    CLONE_DIR="/opt/fast-vpn-business"

    if ! command -v git >/dev/null 2>&1; then
        echo "git не установлен. Установите git или запустите install.sh из полного клона репозитория." >&2
        exit 1
    fi

    if [[ -d "$CLONE_DIR/.git" ]]; then
        echo "Обновляю $CLONE_DIR ..."
        git -C "$CLONE_DIR" pull --ff-only 2>&1 || echo "Не удалось обновить (нет интернета?), запускаю текущую версию."
    else
        echo "Скачиваю Fast VPN Business в $CLONE_DIR ..."
        git clone --depth 1 "$REPO_URL" "$CLONE_DIR" || {
            echo "Не удалось склонировать $REPO_URL." >&2
            exit 1
        }
    fi
    exec bash "$CLONE_DIR/install.sh" "$@"
fi

# shellcheck source=modules/common.sh
source "$SCRIPT_DIR/modules/common.sh"
# shellcheck source=modules/install_panel.sh
source "$SCRIPT_DIR/modules/install_panel.sh"
# shellcheck source=modules/install_node.sh
source "$SCRIPT_DIR/modules/install_node.sh"
# shellcheck source=modules/install_selfsteal.sh
source "$SCRIPT_DIR/modules/install_selfsteal.sh"
# shellcheck source=modules/install_bot.sh
source "$SCRIPT_DIR/modules/install_bot.sh"
# shellcheck source=modules/cleanup.sh
source "$SCRIPT_DIR/modules/cleanup.sh"

main_menu() {
    while true; do
        print_banner
        echo -e "${C_BOLD}Выберите действие:${C_RESET}"
        echo "  1) Установить Remnawave Panel + Subscription Page"
        echo "  2) Подключить этот сервер как ноду"
        echo "  3) Caddy Selfsteal (маскировка Reality для ноды)"
        echo "  4) Установить Telegram-бота (white-label)"
        echo "  5) Полная установка на этом сервере (панель + sub-page + бот)"
        echo "  6) Очистка / удаление установленного"
        echo "  7) Выход"
        echo
        local choice
        read -r -p "$(echo -e "${C_BOLD}>${C_RESET} ")" choice
        case "$choice" in
            1) require_root; detect_os; install_panel ;;
            2) require_root; detect_os; install_node ;;
            3) require_root; detect_os; selfsteal_menu ;;
            4) require_root; detect_os; install_bot ;;
            5)
                require_root; detect_os
                install_panel
                install_bot
                ;;
            6) require_root; cleanup_menu ;;
            7)
                echo "До встречи! Открыть меню снова: sudo bash /opt/fast-vpn-business/install.sh"
                exit 0
                ;;
            *) log_warn "Некорректный выбор."; sleep 1 ;;
        esac
    done
}

main_menu
