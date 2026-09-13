# Деплой

Статус: ФАЗА 0. Никакие действия на VPS в этой фазе не выполняются — документ
описывает целевую схему и процедуры. Всё, что касается реального VPS, шлюза,
DNS и сертификатов, выполняет владелец по этому плану (SPEC §0.3).

---

## 1. Исходное состояние VPS (аудит владельца, 2026-08-31)

- Ubuntu 24.04.1 LTS, ядро 6.8, Hyper-V; 2 vCPU, 3.8 GiB RAM, диск 124 GB
  (занято 12%); Docker 29.4.2; ufw: 80, 443, 22.
- **Nginx существует только в Docker**: контейнер `app-gateway-nginx`
  (nginx:alpine) держит 80/443, терминирует TLS, маршрутизирует по SNI/Host
  на `mmedet.kz` (mercury-medet-app) и `qr.bva.kz` (bva). На хосте nginx нет.
- Шлюз монтирует `/etc/letsencrypt:ro` и отдаёт
  `/.well-known/acme-challenge/` из `/var/www/certbot`.
- Certbot 2.9.0 на хосте. Механизм webroot готов (шлюз отдаёт
  `/.well-known/acme-challenge/` из `/var/www/certbot`) — им и пользуемся
  для нового домена. Состояние сертификатов существующих доменов — зона
  ответственности владельца, вне этого документа.
- Сети: `mercury-medet-app_default` (шлюз + mercury + алиас bva-frontend),
  `best-volleyball-academy_default`, `volleyball-sync_sync_private`
  (internal). Наружу опубликованы только 127.0.0.1:8081/8082 (отладка).
- Бэкапов по расписанию нет; дампы вручную в `~/deploy-backups/`.

Ограничение SPEC (инвариант 15): не ломать mmedet.kz, qr.bva.kz, их
контейнеры и БД, общий шлюз, порты 80/443/22.

## 2. Наш compose-проект

Имя проекта — из `COMPOSE_PROJECT_NAME` (дефолт `wabot`), без упоминания
компании: код white-label, вторая инсталляция = второй каталог + свой `.env`.

### 2.1 Сервисы

| Сервис | Образ | Память (limit) | CPU | Назначение |
|---|---|---|---|---|
| postgres | pgvector/pgvector:pg16 (digest-pinned) | 512 MiB | 0.5 | БД + pgvector |
| redis | redis:7-alpine (digest-pinned) | 96 MiB | 0.2 | locks, debounce, rate limits |
| backend | свой, multi-stage, non-root | 320 MiB | 0.5 | FastAPI (админ-API) |
| worker | тот же образ, другая команда | 384 MiB | 0.75 | polling, pipeline, планировщики |
| frontend | nginxinc/nginx-unprivileged:1.27-alpine + статика | 32 MiB | 0.1 | React-статика, прокси /api; non-root, слушает 8080 |

Итого лимитов: ~1.34 GiB — в пределах разрешённых 1.2–1.5 GiB (SPEC §16),
существующие контейнеры не выдавливаются (у хоста ~3 GiB свободно + swap).

Обоснование цифр: PostgreSQL — `shared_buffers=128MB`, `max_connections=30`,
`work_mem=8MB` (два наших процесса с небольшими пулами; HNSW-индекс на сотнях
чанков помещается в кэш целиком); Redis хранит только ключи координации —
`maxmemory 64mb`, `maxmemory-policy noeviction` (потеря локов/лимитов хуже,
чем отказ записи); worker крупнее backend из-за сжатия изображений
(в thread pool) и SDK.

Общие требования ко всем сервисам: `restart: unless-stopped`, healthcheck,
`cap_drop: [ALL]`, non-root `USER`, `read_only` rootfs где возможно
(+tmpfs для временных файлов), логи json-file с ротацией
(`max-size: 10m`, `max-file: 3`).

### 2.2 Сети и порты

```
wabot_internal (internal: true):  postgres, redis, backend, worker, frontend
wabot_edge:                       frontend + app-gateway-nginx (подключается владельцем)
```

- **Ни один порт не публикуется на 0.0.0.0** (SPEC инвариант 13). Для
  отладки допустим `127.0.0.1:8083 → frontend` по образцу существующих
  8081/8082 — по умолчанию выключен.
- БД и Redis живут только в `wabot_internal` (internal: true — без выхода
  наружу), шлюз их не видит.
- Подключение шлюза: в каталоге gateway-проекта владелец добавляет
  compose-override, подключающий `app-gateway-nginx` к сети `wabot_edge`.
  Мы не входим в чужую сеть `mercury-medet-app_default` — меньше связности
  с существующими проектами, проще снести/перенести.

### 2.3 Тома

| Том | Содержимое | Замечание |
|---|---|---|
| wabot_pgdata | данные PostgreSQL | |
| wabot_media | медиа из WhatsApp | автоочистка retention'ом (7 дней) |
| bind: /var/backups/wabot | дампы БД | на диске хоста, вне docker volumes |

## 3. Домен, шлюз, TLS

### 3.1 DNS

Владелец создаёт A-запись `bot.mmedet.kz` → IP VPS.

### 3.2 Vhost на шлюзе

Файл `deploy/nginx/bot.mmedet.kz.conf.example` (создаётся в ФАЗЕ 1) —
владелец кладёт его в каталог vhost-конфигов шлюза и перезагружает nginx.
Скелет:

```nginx
server {
    listen 80;
    server_name bot.mmedet.kz;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 443 ssl;
    http2 on;
    server_name bot.mmedet.kz;
    ssl_certificate     /etc/letsencrypt/live/bot.mmedet.kz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.mmedet.kz/privkey.pem;

    # security-заголовки уровня шлюза (HSTS и т.д.) — как в конфиге приложения

    location / {
        proxy_pass http://wabot-frontend;      # имя в сети wabot_edge
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 12m;              # загрузка файлов в базу знаний
    }
}
```

Существующие server-блоки mmedet.kz / qr.bva.kz не трогаются.

### 3.3 Сертификат — webroot, без конфликта за порт 80

Механизм уже готов: шлюз отдаёт `/.well-known/acme-challenge/` из
`/var/www/certbot`, certbot стоит на хосте. Выпуск:

```bash
certbot certonly --webroot -w /var/www/certbot -d bot.mmedet.kz \
  --deploy-hook 'docker exec app-gateway-nginx nginx -s reload'
```

Никакого standalone: порт 80 занят шлюзом, certbot в standalone-режиме не
сможет подняться.

Для автообновления нашего сертификата на хосте должен быть активен
certbot-таймер (`certbot.timer`) — проверить при деплое. `--deploy-hook`
из команды выше сохраняется в renewal-конфиге и перезагрузит шлюз после
каждого обновления.

## 4. Healthchecks

| Сервис | Проверка |
|---|---|
| postgres | `pg_isready` |
| redis | `redis-cli ping` |
| backend | HTTP `GET /healthz` → `{"status":"ok"}` (ничего чувствительного) |
| worker | liveness-файл/heartbeat-ключ в Redis, обновляемый циклом; проверка свежести |
| frontend | `GET /` → 200 |

Worker дополнительно репортит в `system_events`: глубина очереди, ошибки
polling, backlog GREEN-API — это видно на странице «Система» и уходит в
WhatsApp-алерты.

## 5. Бэкапы **[РЕШЕНИЕ: хранение только на самом сервере]**

- `deploy/backup/pg_backup.sh`: `pg_dump -Fc` внутрь контейнера →
  `/var/backups/wabot/wabot-YYYY-MM-DD.dump`, права 600. Скрипт пишет
  факт успешной копии в `system_events` — иначе страница «Система» не
  может показать, когда была последняя копия, а воркер не может
  заметить, что бэкапы тихо перестали делаться.
- Расписание: `deploy/systemd/wabot-backup.timer` (03:30, Persistent) —
  ставит владелец: `cp deploy/systemd/wabot-backup.* /etc/systemd/system/
  && systemctl enable --now wabot-backup.timer`. Альтернатива для cron —
  `deploy/systemd/crontab.example`.
- Ротация: 7 дневных, 4 недельных, 3 месячных. При ~340 МБ суммарных БД
  соседей и нашей маленькой базе место не проблема; скрипт всё равно
  проверяет свободное место перед запуском.
- `deploy/backup/pg_restore.sh`: восстановление в остановленное приложение
  (`pg_restore --clean --if-exists`), с обязательным подтверждением.
- Медиа-том: в бэкап не входит (файлы эфемерны, retention 7 дней;
  транскрипты — в БД).
- Проверка восстановимости: `make backup-verify`
  (`deploy/backup/backup_verify.sh`) восстанавливает свежий дамп во
  ВРЕМЕННУЮ базу в том же контейнере и проверяет не только `count(*)`, но
  и версию схемы, суммы расходов и целость векторов pgvector. Живая база
  не затрагивается. В cron-примере — раз в квартал.
- `.env` в бэкап НЕ входит **[РЕШЕНИЕ 2026-09-14]**: `pg_dump` не
  содержит паролей ролей, а положить ключи OpenAI и GREEN-API рядом с
  персональными данными клиентов — значит из одной украденной папки
  получить и переписку, и возможность тратить деньги. Отдельно от
  сервера владелец хранит только `ENCRYPTION_KEY` (без него нечитаем
  зашифрованный TOTP-секрет) и коды восстановления.

Зафиксированный риск: offsite-копий нет по решению владельца. Смерть диска
VPS = потеря истории. Скрипт `offsite_sync.sh` из SPEC-структуры остаётся
заготовкой с комментарием «выключено решением владельца от 2026-08-31».

Мониторинг (реализовано в фазе 9, `services/monitoring.py`, раз в 5 минут):
диск (порог настраивается, по умолчанию 85%), глубина очереди, свежесть
резервной копии. Всё пишется в `system_events` и уходит алертом в WhatsApp.
Память контейнеров не мониторится: изнутри read-only контейнера без
доступа к docker.sock достоверной цифры нет — смотреть `docker stats` на
хосте.

Ретеншен базы (фаза 9, `services/retention.py`, раз в 6 часов) реализует
политику §5 docs/security-and-cost-controls.md; сроки правятся в разделе
«Лимиты» без правки кода.

## 6. Конфигурация

- `.env` (права 600, в `.dockerignore` и `.gitignore`): секреты
  (OPENAI_API_KEY, GREEN_API_TOKEN, ENCRYPTION_KEY, SESSION_SECRET) и
  стартовые значения настроек. `.env.example` — только ключи без значений.
- Всё, что можно менять на лету (лимиты, модели, пороги, alert_phone,
  org_name), после первого старта живёт в таблице `settings` и правится
  через админку; `.env` — начальные значения.
- `DRY_RUN=true` — дефолт до приёмки: сообщения принимаются, RAG и AI
  работают и логируются, отправка в WhatsApp не выполняется.

## 7. Процедура первого деплоя (выполняет владелец)

1. DNS: A-запись `bot.mmedet.kz`.
2. `git clone` в каталог на VPS, `cp .env.example .env`, заполнить, `chmod 600 .env`.
3. `make up` (compose up -d: postgres, redis) → `make migrate` → `make create-admin`.
4. Поднять backend/worker/frontend: `make up` (полный).
5. Сертификат по webroot (см. 3.3), vhost-конфиг в шлюз, подключить шлюз к
   `wabot_edge`, `nginx -s reload` в контейнере шлюза.
6. Войти в админку, настроить TOTP, сохранить recovery-коды.
7. Указать alert_phone, проверить страницу «Система» (GREEN-API state,
   OpenAI ping).
8. Прогнать в `DRY_RUN=true` на реальном трафике ≥1 день, смотреть логи
   и расходы.
9. Выключить DRY_RUN.

## 8. Обновление и откат

Обновление:
```
git pull
make build            # docker compose build (образы тегируются git SHA)
make migrate          # alembic upgrade head
make up               # пересоздание изменившихся сервисов
make smoke            # healthz + статус страницы Система
```

Откат:
- приложение: `docker compose up -d` с предыдущим тегом образа (теги = git
  SHA, хранится минимум 2 предыдущих);
- миграции: `alembic downgrade` на одну ревизию (каждая миграция обязана
  иметь рабочий down — SPEC §18);
- катастрофа: восстановление дампа через `pg_restore.sh` + предыдущий образ.

Обновления не выполняются при непройденном CI (ruff, mypy, bandit,
pip-audit, тесты, npm audit, gitleaks, trivy).

## 9. Что владелец делает руками (сводка)

DNS-запись; правки шлюза (vhost + сеть + reload); выпуск сертификата;
установка cron бэкапов; заполнение `.env`; `make create-admin`; выключение
DRY_RUN. Всё остальное — код и скрипты репозитория.
