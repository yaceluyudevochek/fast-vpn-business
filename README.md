# Fast VPN Business

Инструмент для разворачивания VPN-бизнеса под ключ на Linux: панель [Remnawave](https://docs.rw), ноды, Subscription Page и white-label Telegram-бот для продажи подписок — одной командой, через понятное консольное меню.

## Что устанавливает

| Компонент              | Что делает                                                                 |
|-------------------------|------------------------------------------------------------------------------|
| **Remnawave Panel**      | Основная панель управления пользователями/нодами (Docker Compose, официальная схема). |
| **Reverse Proxy (Caddy)** | Автоматический HTTPS для домена панели и sub-страницы.                     |
| **Subscription Page**    | Публичная страница подписки, скрывающая домен панели от пользователей.     |
| **Node**                 | Подключение VPN-сервера (Xray-core) к панели.                              |
| **Caddy Selfsteal**      | Маскировка Reality-трафика: настоящий HTTPS-сайт (один из 11 шаблонов) на порту, который Reality использует как `dest`. |
| **Telegram-бот**         | Продажа подписок: тарифы, триал, реферальная программа, оплата через Platega.io, админ-панель. White-label — без единой готовой картинки или названия внутри. |

## Быстрый старт

На чистом сервере Ubuntu/Debian (root):

```bash
curl -fsSL https://raw.githubusercontent.com/yaceluyudevochek/fast-vpn-business/main/install.sh -o install.sh
sudo bash install.sh
```

Появится меню:

```
1) Установить Remnawave Panel + Subscription Page
2) Подключить этот сервер как ноду
3) Caddy Selfsteal (маскировка Reality для ноды)
4) Установить Telegram-бота (white-label)
5) Полная установка на этом сервере (панель + sub-page + бот)
6) Очистка / удаление установленного
7) Выход
```

Панель, ноды и бот почти всегда живут на разных серверах — установщик поэтому не "всё-в-одном-запуске", а спрашивает на каждой машине, какую роль она выполняет. Запустите `install.sh` на каждом сервере и выберите нужный пункт.

### Типичный сценарий (3 сервера)

1. **Сервер панели** — пункт `1`. Введите домен панели (и, при желании, домен sub-страницы). Установщик сам поднимет Docker, панель, Caddy с автоматическим SSL и (опционально) Subscription Page.
2. **Сервер(ы) ноды** — пункт `2`. Сначала добавьте ноду в панели (Nodes → Management → `+`), скопируйте сгенерированный `docker-compose.yml`, вставьте его в терминал по подсказке скрипта. Так же, как рекомендует [официальная документация Remnawave](https://docs.rw/install/remnawave-node). На этом же сервере пунктом `3` можно сразу поднять Caddy Selfsteal для маскировки Reality.
3. **Сервер бота** — пункт `4`. Мастер настройки спросит токен бота, адрес и API-ключ панели, реквизиты Platega.io и публичные ссылки (оферта, поддержка) — соберёт `config/config.py` и поднимет systemd-сервис `fastvpnbot`.

Если всё нужно на одном сервере — пункт `5`.

## White-label бот

Бот в `bot/` — без какого-либо жёстко зашитого бренда:

- Ни одно сообщение не содержит названия сервиса — работайте под любым именем.
- Картинки экранов (старт, тарифы, оплата, подписки, информация, рефералка) — **опциональны**. Без них бот работает как обычные текстовые сообщения; см. [bot/assets/images/README.md](bot/assets/images/README.md), чтобы включить свои.
- Все публичные ссылки (оферта, политика конфиденциальности, поддержка) настраиваются мастером установки или прямо в `config/config.py`.

### Возможности бота

- Тарифы (лимитные и безлимитные), пробный период, продление активной/истёкшей подписки.
- Реферальная программа.
- Оплата через Platega.io (СБП, криптовалюта) с идемпотентной обработкой webhook'ов — повторный callback от платёжного шлюза не продлевает подписку дважды.
- Автонапоминания об истечении подписки (за 24ч / 1ч / 10 мин / после истечения), без спама при перезапуске бота.
- Админ-панель: статистика, управление пользователями и подписками, бэкапы БД.
- Работает поверх Remnawave API v3 (числовые ID пользователей, актуальная схема эндпоинтов).

## Caddy Selfsteal

Reality-протокол Xray прячет прокси-трафик за настоящим TLS-хендшейком, но ему нужен
реальный сервер за портом `dest`, который ответит настоящим HTTPS всем, кто пришёл
не через Xray-клиента (в том числе active-probing со стороны DPI). Caddy Selfsteal —
это и есть такой сервер: обычный Caddy с настоящим сертификатом, отдающий один из
11 готовых сайтов-прикрытий.

Ставится на **том же сервере, что и нода** (пункт `3`, обычно сразу после пункта `2`):

- Домен для сертификата — должен совпадать с `serverNames` в Reality-инбаунде этой ноды.
- Тип сертификата — обычный (HTTP-01) или wildcard через Cloudflare DNS-01 (токен
  проверяется через Cloudflare API перед использованием).
- HTTPS-порт (по умолчанию `9443`) — это же значение указывается как `dest` в Xray Reality.
- Шаблон сайта — выбирается из 11 готовых вариантов (мемы, конвертер файлов, спидтест,
  видеохостинг, игровой портал и т.д.) или ставится случайно; сменить шаблон можно
  в любой момент без переустановки Caddy (пункт подменю "Сменить HTML-шаблон").

После установки скрипт печатает готовые значения для Reality-инбаунда в панели:

```json
{
  "serverNames": ["your-domain.com"],
  "dest": "127.0.0.1:9443",
  "xver": 0
}
```

## Структура репозитория

```
install.sh              — точка входа, меню
modules/
  common.sh              — цвета, баннер, ввод данных, проверка Docker/ОС
  install_panel.sh        — Remnawave Panel + Caddy + Subscription Page (bundled)
  install_node.sh          — подключение ноды
  install_selfsteal.sh      — Caddy Selfsteal (маскировка Reality) + выбор шаблона
  install_bot.sh              — установка бота + мастер white-label конфигурации
  cleanup.sh                    — полное удаление панели/ноды/selfsteal/бота с этого сервера
bot/                      — исходный код Telegram-бота (aiogram 3)
  handlers/                — обработчики команд/колбэков (пользователь + админ)
  services/                — Remnawave API, Platega API, webhook, напоминалка
  database/                — SQLite (тарифы, подписки, платежи, рефералы)
  config/config.py.example — шаблон конфига (реальный config.py не хранится в git)
  assets/images/            — сюда кладутся картинки экранов (опционально)
caddy-selfsteal/
  templates/                — 11 готовых HTML-шаблонов сайтов-прикрытий (см. SOURCE.md)
```

## Полезные команды

### Сам установщик

```bash
# Первый запуск (curl | bash сам склонирует репозиторий во временный каталог)
curl -fsSL https://raw.githubusercontent.com/yaceluyudevochek/fast-vpn-business/main/install.sh -o install.sh
sudo bash install.sh

# Повторный запуск меню из уже склонированного репозитория
cd fast-vpn-business && sudo bash install.sh

# Обновить установщик и бот до последней версии из git, затем перезапустить меню
cd fast-vpn-business && git pull && sudo bash install.sh

# Полная очистка того, что стоит на этом сервере (пункт меню 6), без диалога:
# просто откройте install.sh и выберите нужный пункт — отдельного флага
# для неинтерактивного вызова нет специально, чтобы нельзя было случайно
# снести панель одной командой в скрипте автоматизации.
```

### Панель (`/opt/remnawave`)

```bash
cd /opt/remnawave
docker compose logs -f -t                 # логи панели
docker compose restart                    # перезапуск панели
docker compose ps                         # статус контейнеров

cd /opt/remnawave/caddy && docker compose logs -f -t   # логи Caddy / выпуска SSL
cd /opt/remnawave/subscription && docker compose logs -f -t   # логи Subscription Page
```

### Нода (`/opt/remnanode`)

```bash
cd /opt/remnanode
docker compose logs -f -t     # логи ноды
docker compose restart        # перезапуск ноды
```

### Caddy Selfsteal (`/opt/caddy`)

```bash
cd /opt/caddy
docker compose logs -f -t     # логи Caddy / выпуска сертификата
docker compose restart        # перезапуск
cat .env                      # текущий домен/порт (SELF_STEAL_DOMAIN, SELF_STEAL_PORT)

# Сменить шаблон сайта без переустановки — через меню install.sh:
# пункт 3 -> "Сменить HTML-шаблон"
```

### Telegram-бот (systemd-сервис `fastvpnbot`)

```bash
systemctl status fastvpnbot           # статус
journalctl -u fastvpnbot -f           # логи в реальном времени
journalctl -u fastvpnbot -n 200       # последние 200 строк
systemctl restart fastvpnbot          # перезапуск (например, после правки config.py)
systemctl stop fastvpnbot             # остановить

# Ручной бэкап базы данных бота
cp /opt/fastvpnbot/bot_database.db /root/bot_database_$(date +%Y%m%d_%H%M%S).db
```

## Требования

- Ubuntu 22.04+/Debian 11+ (рекомендовано, см. [Remnawave requirements](https://docs.rw/install/requirements)).
- Панель: 2+ ГБ RAM, 2+ CPU, 20 ГБ диска.
- Нода: 1+ ГБ RAM, 1+ CPU.
- Зарегистрированный домен для панели (и отдельный — для sub-страницы, если используется).
- Отдельный домен для Caddy Selfsteal (может быть поддоменом), указывающий на IP ноды; для wildcard-сертификата — зона в Cloudflare.
- Аккаунт Platega.io для приёма платежей.

## Безопасность

- `install.sh` работает только под root и не хранит и не передаёт никуда введённые секреты — они остаются локально в `.env`/`config.py` на соответствующем сервере.
- `config/config.py` бота никогда не коммитится (см. `.gitignore`) — используйте `config/config.py.example` как образец.
- Node-порт по умолчанию предлагается ограничить файрволом только для IP панели.
- Очистка (пункт меню 6) необратима: перед реальным удалением каждый шаг требует ввести слово `УДАЛИТЬ` заглавными буквами, а для бота дополнительно предлагает сохранить бэкап `config.py` и базы данных.
- Cloudflare API Token для wildcard-сертификата хранится в `/opt/caddy/.env` (`chmod 600`) и проверяется через Cloudflare API перед использованием.

## Стек

Bash (установщик) · Docker / Docker Compose · Remnawave (NestJS + Xray-core) · Caddy · Python 3.12+, aiogram 3.31, aiosqlite, aiohttp (бот).

## Благодарности

Логика установки Caddy Selfsteal и все 11 HTML-шаблонов маскировки (`caddy-selfsteal/templates/`)
адаптированы из проекта [Case211/remnanode-install](https://github.com/Case211/remnanode-install),
распространяемого по лицензии MIT.
