# Трекер игр Metacritic

Каждый час собирает новые игры с Metacritic и показывает их в браузере: оценки критиков и игроков,
трейлеры, похожие игры и ИИ-сводку отзывов («что понравилось / не понравилось»).

## Запуск

```bash
cp .env.example .env    # впишите OPENROUTER_API_KEY
docker compose up --build
```

Откройте <http://localhost:8000>. Первый сбор начнётся в начале следующего часа. Чтобы не ждать, нажмите **«Запустить сбор»**.

Без ключа OpenRouter всё работает, кроме ИИ-сводок.

## Что где

- **Игры** (`/`): новинки, поиск, фильтр по платформе и сортировка.
- **Страница игры**: оценки по платформам, описание, трейлер, похожие игры и ИИ-сводка.
  Сводка создаётся при первом открытии игры (несколько секунд) и обновляется, только когда появляются новые отзывы.
- **Мониторинг** (`/dashboard`): текущий сбор, воркеры, история запусков и ошибки.

## Настройки

Задаются в `.env`. Основные:

| Переменная | По умолчанию | Что меняет |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Ключ для ИИ-сводок |
| `SUMMARY_LANGUAGE` | `Russian` | Язык сводок |
| `GAMES_PER_RUN` | `500` | Сколько игр собирать за запуск |
| `TIMEZONE` | `Europe/Moscow` | Часовой пояс расписания и дат |
| `WEB_PORT` | `8000` | Порт сайта |

Остальные параметры описаны в `.env.example`.

## Если что-то не так

- **`all predefined address pools have been fully subnetted`** при запуске (обычно из-за VPN):
  выполните `cp docker-compose.override.example.yml docker-compose.override.yml`.
- **Сводка не появляется**: проверьте `OPENROUTER_API_KEY` и раздел ошибок в мониторинге.
- **Сбор прервался**: просто запустите его снова. Он продолжит с того места, где остановился.

## Запуск на сервере (VPS)

Нужен сервер с Ubuntu, 2 ГБ памяти и 20 ГБ диска. Сайт открывается через Caddy, который сам
получает HTTPS-сертификат для вашего домена.

1. Если есть домен, направьте его A-запись на IP сервера.
2. Подключитесь к серверу (`ssh root@<IP>`) и установите Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. Скачайте проект и создайте `.env`:
   ```bash
   git clone https://github.com/kostindata/metacritic-game-tracker.git /opt/metacritic-game-tracker
   cd /opt/metacritic-game-tracker
   cp .env.example .env
   ```
   В `.env` раскомментируйте блок «Server (VPS) only» и укажите `OPENROUTER_API_KEY`,
   `POSTGRES_PASSWORD` (например, из `openssl rand -hex 24`) и `SITE_ADDRESS` — ваш домен
   или `:80`, чтобы открывать сайт по IP без HTTPS.
4. Запустите:
   ```bash
   docker compose up -d --build
   ```
5. Включите ежедневную резервную копию базы (хранятся последние 14 в `backups/`):
   ```bash
   (crontab -l 2>/dev/null; echo "0 4 * * * /opt/metacritic-game-tracker/deploy/backup.sh >> /var/log/mc-backup.log 2>&1") | crontab -
   ```

6. Включите автоматическое обновление: каждые 5 минут сервер проверяет ветку `main` на GitHub
   и, если появились новые коммиты, собирает новую версию и запускает тесты. Новая версия
   заменяет работающую, только если тесты прошли:
   ```bash
   cp deploy/mc-update.service deploy/mc-update.timer /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now mc-update.timer
   ```
   Журнал обновлений: `journalctl -u mc-update`. Обновить вручную прямо сейчас:
   `deploy/update.sh --force`.

На сервере с маленьким диском (10 ГБ) добавьте в `.env` `INSTALL_PLAYWRIGHT=false` и
`PLAYWRIGHT_ENABLED=false`: образ станет меньше на 1,3 ГБ. Этот запасной браузер нужен, только
если Metacritic отдаёт страницу без данных.

Восстановление из копии: `zcat backups/<файл>.sql.gz | docker compose exec -T postgres psql -U mc -d mc`.

## Тесты

```bash
docker compose run --rm --no-deps web pytest
```
