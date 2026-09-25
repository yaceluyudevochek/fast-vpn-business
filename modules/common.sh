#!/usr/bin/env bash
# Общие переменные, цвета и хелперы для всех модулей Fast VPN Business.
# Подключается через `source modules/common.sh` из install.sh и других модулей.

set -uo pipefail

# ---------------------------------------------------------------------------
# Цвета
# ---------------------------------------------------------------------------
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_DIM='\033[2m'
C_RED='\033[0;31m'
C_GREEN='\033[0;32m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'
C_CYAN='\033[0;36m'
C_MAGENTA='\033[0;35m'

log_info()  { echo -e "${C_CYAN}[i]${C_RESET} $*"; }
log_ok()    { echo -e "${C_GREEN}[✓]${C_RESET} $*"; }
log_warn()  { echo -e "${C_YELLOW}[!]${C_RESET} $*"; }
log_err()   { echo -e "${C_RED}[✗]${C_RESET} $*" >&2; }
log_step()  { echo -e "\n${C_BOLD}${C_BLUE}==>${C_RESET} ${C_BOLD}$*${C_RESET}"; }

die() { log_err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Баннер
# ---------------------------------------------------------------------------
print_banner() {
    clear
    echo -e "${C_CYAN}${C_BOLD}"
    cat <<'EOF'
   ______           __     _    ______  _   __   ____             _
  / ____/___ ______/ /_   | |  / / __ \/ | / /  / __ )__  _______(_)___  ___  __________
 / /_  / __ `/ ___/ __/   | | / / /_/ /  |/ /  / __  / / / / ___/ / __ \/ _ \/ ___/ ___/
/ __/ / /_/ (__  ) /_     | |/ / ____/ /|  /  / /_/ / /_/ (__  ) / / / /  __(__  |__  )
/_/    \__,_/____/\__/     |___/_/   /_/ |_/  /_____/\__,_/____/_/_/ /_/\___/____/____/
EOF
    echo -e "${C_RESET}${C_DIM}          Turnkey VPN business deployment toolkit — Remnawave + Telegram bot${C_RESET}\n"
}

# ---------------------------------------------------------------------------
# Проверки окружения
# ---------------------------------------------------------------------------
require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "Запустите скрипт от root (sudo -i, затем повторите команду)."
    fi
}

detect_os() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_VERSION="${VERSION_ID:-unknown}"
    else
        OS_ID="unknown"
        OS_VERSION="unknown"
    fi

    if [[ "$OS_ID" != "ubuntu" && "$OS_ID" != "debian" ]]; then
        log_warn "Скрипт тестировался на Ubuntu/Debian. Обнаружена ОС: $OS_ID $OS_VERSION."
        confirm "Продолжить всё равно?" "n" || exit 1
    fi
}

# ---------------------------------------------------------------------------
# Ввод данных
# ---------------------------------------------------------------------------

# ask "Вопрос" "значение_по_умолчанию" -> печатает ответ в stdout
ask() {
    local prompt="$1" default="${2:-}" answer
    if [[ -n "$default" ]]; then
        read -r -p "$(echo -e "${C_BOLD}?${C_RESET} ${prompt} ${C_DIM}[${default}]${C_RESET}: ")" answer
        echo "${answer:-$default}"
    else
        while [[ -z "${answer:-}" ]]; do
            read -r -p "$(echo -e "${C_BOLD}?${C_RESET} ${prompt}: ")" answer
            [[ -z "$answer" ]] && log_warn "Значение обязательно, повторите ввод."
        done
        echo "$answer"
    fi
}

# ask_secret "Вопрос" -> печатает ответ в stdout, не отображая ввод на экране
ask_secret() {
    local prompt="$1" answer
    while [[ -z "${answer:-}" ]]; do
        read -r -s -p "$(echo -e "${C_BOLD}?${C_RESET} ${prompt}: ")" answer
        echo
        [[ -z "$answer" ]] && log_warn "Значение обязательно, повторите ввод."
    done
    echo "$answer"
}

# confirm "Вопрос" "y|n" -> возвращает 0 (да) / 1 (нет) как код возврата
confirm() {
    local prompt="$1" default="${2:-y}" answer hint
    [[ "$default" == "y" ]] && hint="Y/n" || hint="y/N"
    read -r -p "$(echo -e "${C_BOLD}?${C_RESET} ${prompt} [${hint}]: ")" answer
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
ensure_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log_ok "Docker и Docker Compose уже установлены."
        return
    fi
    log_step "Устанавливаю Docker"
    curl -fsSL https://get.docker.com | sh || die "Не удалось установить Docker."
    systemctl enable --now docker >/dev/null 2>&1 || true
    log_ok "Docker установлен."
}

pause() {
    read -r -p "$(echo -e "${C_DIM}Нажмите Enter, чтобы продолжить...${C_RESET}")"
}
