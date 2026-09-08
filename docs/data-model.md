# Модель данных

Статус: ФАЗА 0. PostgreSQL 16 + pgvector. Все таблицы создаются миграциями
Alembic (вверх и вниз). Здесь — полная схема; обязательный минимум из SPEC §5
сохранён, расширения помечены **[РАСШИРЕНИЕ]** с обоснованием.

Общие соглашения:
- `id` — `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`, если не указано иное.
- Все таймстемпы — `timestamptz`, UTC. Отображение в часовом поясе организации
  (`settings.timezone`, дефолт `Asia/Almaty`) — забота UI.
- Телефоны хранятся в E.164 (`+7701...`), нормализация на сервере.
- ENUM'ы — PostgreSQL enum-типы (перечислены в §20).
- Ни в одной таблице не хранятся секреты в открытом виде: только хэши
  (`password_hash`, `session_token_hash`, `recovery_code_hash`) или шифртекст
  (`totp_secret_encrypted`, ключ шифрования — из env), или ссылки
  (`token_ref` — имя env-переменной).

---

## 1. `admin_user`

| Поле | Тип | Примечание |
|---|---|---|
| id | bigint PK | |
| phone | text NOT NULL | E.164 |
| full_name | text NOT NULL | |
| password_hash | text NOT NULL | argon2id |
| is_active | bool NOT NULL default true | |
| totp_secret_encrypted | text NULL | шифруется ключом из env |
| failed_login_count | int NOT NULL default 0 | |
| locked_until | timestamptz NULL | прогрессивная блокировка |
| password_changed_at | timestamptz NOT NULL | |
| created_at / updated_at | timestamptz NOT NULL | |

Индексы: `UNIQUE (phone)` — телефон является логином, двух учёток с одним
номером быть не может.

Правило «ровно один администратор»: приложение при старте выполняет
`SELECT count(*) FROM admin_user WHERE is_active` и падает с внятной ошибкой,
если активных записей больше одной (SPEC §5). Ролей нет.

## 2. `admin_totp_recovery_codes` **[РАСШИРЕНИЕ]**

SPEC §10 требует 8 одноразовых кодов восстановления, хранящихся хэшами —
им нужна таблица.

| Поле | Тип |
|---|---|
| id | bigint PK |
| user_id | FK → admin_user |
| code_hash | text NOT NULL |
| used_at | timestamptz NULL |
| created_at | timestamptz NOT NULL |

Индекс: `(user_id) WHERE used_at IS NULL` — выборка активных кодов при входе.

## 2a. `login_challenges` **[РАСШИРЕНИЕ]**

Промежуточное состояние «пароль принят, ждём TOTP». Сознательно НЕ сессия:
обладание challenge-токеном не даёт доступа ни к одному API (покрыто тестом).

| Поле | Тип |
|---|---|
| id | bigint PK |
| user_id | FK → admin_user |
| token_hash | text NOT NULL |
| ip | text NULL |
| expires_at | timestamptz NOT NULL (TTL ~5 мин) |
| consumed_at | timestamptz NULL (одноразовость) |
| created_at | timestamptz NOT NULL |

Индекс: `UNIQUE (token_hash)` — поиск по хэшу токена, защита от коллизий.

## 3. `admin_sessions`

id, user_id FK, session_token_hash (НЕ сам токен), user_agent, ip,
created_at, last_seen_at, expires_at, revoked_at.

Индексы: `UNIQUE (session_token_hash)` — поиск сессии по хэшу токена из
cookie, O(1) и защита от коллизий; `(user_id, expires_at)` — «завершить все
сессии» и чистка истёкших.

## 4. `audit_log`

id, actor_user_id (NULL для системных действий), action, entity_type,
entity_id, before jsonb, after jsonb, ip, correlation_id, created_at.
Без секретов и без текстов клиентов.

Индексы: `(created_at)`, `(entity_type, entity_id)`, `(action)` — фильтры
экрана «Журнал» (§12.8 SPEC).

## 5. `whatsapp_instances`

id, provider (`green_api`), instance_id text, token_ref text (имя
env-переменной, НЕ токен), is_active bool, last_polled_at, last_error text.

Индекс: `UNIQUE (provider, instance_id)` — один и тот же инстанс нельзя
завести дважды, иначе polling задвоится и DeleteNotification одного потока
съест уведомления другого. Практически строка одна [РЕШЕНИЕ владельца:
инстанс один], мультиинстансный UI не делается.

## 6. `inbound_notifications`

id, instance_id FK, provider_message_id text, notification_type text,
raw_payload jsonb, received_at, processed_at, status
(`stored`/`processed`/`ignored`/`failed`).

Индексы:
- `UNIQUE (instance_id, provider_message_id)` — **главная защита от повторной
  генерации** (SPEC инвариант 3): повторная доставка того же уведомления
  упирается в constraint и не порождает второй вызов OpenAI. Констрейнт в БД,
  а не проверка в памяти — рестарт процесса ничего не обнуляет.
- `(status, received_at)` — выборка необработанных и чистка по retention.

`raw_payload` содержит текст клиента → персональные данные. Retention:
поле обнуляется планировщиком через `RETENTION_RAW_PAYLOAD_DAYS` (дефолт 30)
после `processed_at`; строка с метаданными остаётся для дедупликации.
Групповые уведомления получают `status=ignored` без создания чата
[РЕШЕНИЕ: бот в группах не читает и не пишет].

## 6a. `wa_outbox` **[РАСШИРЕНИЕ]**

Транспортный outbox: каждая исходящая отправка в WhatsApp проходит через
него. Доменная таблица `messages` (фаза 3) ссылается на тот же
`outbound_message_id` — транспорт остаётся независимым от домена чатов.

| Поле | Тип | Примечание |
|---|---|---|
| id | bigint PK | |
| instance_id | FK → whatsapp_instances | |
| outbound_message_id | uuid NOT NULL | наш идемпотентный ключ |
| wa_chat_id | text NOT NULL | |
| kind | text | `text`/`file` |
| body | text NULL | текст или подпись файла |
| file_ref | text NULL | путь в media-томе для kind=file |
| state | text | `PENDING` → `SENT` / `SKIPPED_DRY_RUN` / `FAILED_RETRYABLE` → `FAILED_FINAL` |
| retry_count / next_attempt_at | | backoff+jitter, максимум `RETRY_MAX_ATTEMPTS` |
| provider_message_id | text NULL | idMessage от провайдера после отправки |
| error_code / error_detail | text NULL | |
| correlation_id | text NULL | |
| created_at / sent_at | timestamptz | |

Индексы:
- `UNIQUE (outbound_message_id)` — идемпотентность отправки: повторный
  enqueue или ретрай flush не может отправить сообщение дважды.
- частичный `(next_attempt_at) WHERE state IN ('PENDING','FAILED_RETRYABLE')`
  — рабочая выборка флашера без сканирования отправленного архива.

В `DRY_RUN=true` строки финализируются как `SKIPPED_DRY_RUN` — обработано и
залогировано, наружу не ушло (режим приёмки, SPEC §17).

Дедупликация статусов доставки: несколько статусов одного сообщения
(sent → delivered → read) имеют один idMessage, поэтому в
`inbound_notifications.provider_message_id` для статусов кладётся составной
ключ `{idMessage}:status:{status}` — инвариант «одно входящее сообщение =
максимум один вызов AI» сохраняется, а статусы не теряются.

## 7. `chats`

| Поле | Тип | Примечание |
|---|---|---|
| id | bigint PK | |
| instance_id | FK → whatsapp_instances | |
| wa_chat_id | text NOT NULL | только личные чаты, `@g.us` сюда не попадает |
| phone | text NOT NULL | E.164 |
| display_name | text NULL | |
| mode | chat_mode NOT NULL default `AUTO_AI` | |
| paused_until | timestamptz NULL | |
| mode_changed_by | text NULL | `system`/`operator_panel`/`operator_phone`/`scheduler` |
| mode_changed_at | timestamptz NULL | |
| mode_change_reason | text NULL | |
| is_resolved | bool NOT NULL default false | |
| unread_count | int NOT NULL default 0 | |
| last_message_at | timestamptz NULL | |
| last_actor | text NULL | `ai`/`human`/`customer` |
| total_cost_usd | numeric(12,6) NOT NULL default 0 | |
| needs_attention | bool NOT NULL default false | **[РАСШИРЕНИЕ]** флаг «требует внимания» — замена сущности «Заявка» [РЕШЕНИЕ] |
| attention_reason | attention_reason NULL | `handoff`/`processing_error`/`blocked_by_limit`/`manual` |
| attention_set_at | timestamptz NULL | |
| attention_cleared_at | timestamptz NULL | |
| attention_cleared_reason | text NULL | `operator_phone_reply`/`panel_opened`/`manual` |
| created_at | timestamptz NOT NULL | |

Индексы:
- `UNIQUE (instance_id, wa_chat_id)` — один WhatsApp-собеседник = ровно один
  чат; гонка двух параллельных уведомлений от нового клиента разрешается
  constraint'ом (`INSERT ... ON CONFLICT`), а не проверкой в коде.
- `(last_message_at DESC)` — список чатов, cursor-пагинация.
- `(needs_attention) WHERE needs_attention` — частичный, фильтр «требует
  внимания» без сканирования архива.
- `(mode, paused_until) WHERE mode = 'PAUSED_UNTIL'` — частичный, планировщик
  возврата в AUTO_AI находит просроченные паузы мгновенно.

## 8. `chat_mode_history`

id, chat_id FK, from_mode, to_mode, actor_user_id NULL, reason,
paused_until, created_at. Индекс `(chat_id, created_at)`.

## 9. `messages`

| Поле | Тип | Примечание |
|---|---|---|
| id | bigint PK | |
| chat_id | FK → chats | |
| direction | `in`/`out` | |
| source | msg_source | `customer`/`ai`/`operator_panel`/`operator_phone`/`system` |
| provider_message_id | text NULL | id сообщения WhatsApp |
| outbound_message_id | uuid NULL | наш идемпотентный ключ отправки |
| body | text NULL | |
| media_type | media_type NULL | `voice`/`image`/`video`/`gif`/`sticker`/`document`/`other` |
| media_ref | text NULL | путь в media-томе, никогда не сырой путь провайдера |
| transcript | text NULL | голосовые: остаётся после удаления файла |
| state | msg_state | см. §20 |
| retry_count | int NOT NULL default 0 | |
| next_attempt_at | timestamptz NULL | бэкофф ретраев пайплайна; у PROCESSING — lease для sweeper'а (фаза 6) |
| error_code / error_detail | text NULL | |
| correlation_id | text NOT NULL | сквозной до OpenAI и отправки |
| delivery_status | text NULL | из статус-уведомлений |
| created_at / sent_at | timestamptz | |

Индексы:
- `UNIQUE (chat_id, provider_message_id) WHERE provider_message_id IS NOT NULL`
  — повторная доставка входящего не создаёт второе сообщение; частичный,
  потому что у наших неотправленных исходящих provider_message_id ещё нет.
- `UNIQUE (outbound_message_id) WHERE outbound_message_id IS NOT NULL` —
  идемпотентность отправки: ретрай шага «send» не может породить дубль.
- `(chat_id, id)` — история чата с cursor-пагинацией.
- `(state) WHERE state IN ('QUEUED','PROCESSING','FAILED_RETRYABLE')` —
  частичный, рабочая выборка dispatcher'а; полный индекс по state бесполезен
  (99% строк в терминальных состояниях).

Стикеры сохраняются с `state=IGNORED` без ответа [РЕШЕНИЕ].

## 10. `chat_memory`

chat_id PK/FK, language (`ru`/`kk`/`en`/`mixed`), interest, requested_date,
requested_time, client_name, handoff_required bool, last_topic, summary_text,
summary_tokens int, updated_at. Одна строка на чат, обновляется дёшево
(правила/regex/микро-вызов), не полным LLM-проходом.

## 11. `internal_notes`

id, chat_id FK, user_id FK, body, created_at. Клиенту не видны никогда.
Индекс `(chat_id, created_at)`.

## 12. `tags` / `chat_tags`

`tags`: id, name, color, created_at. `UNIQUE (lower(name))` — «Аренда» и
«аренда» не должны стать двумя тегами.
`chat_tags`: chat_id FK, tag_id FK, `PRIMARY KEY (chat_id, tag_id)` — тег на
чате либо есть, либо нет; PK и есть дедупликация. Индекс `(tag_id)` —
фильтр чатов по тегу.

## 13. `kb_documents`

id, title, category text, language (`ru`/`kk`/`en`), source_type
(`manual_text`/`file`/`faq`), status (`draft`/`active`/`archived`), version
int, valid_until timestamptz NULL, is_searchable bool, body text,
file_ref text NULL, author_user_id FK NULL, deleted_at timestamptz NULL,
created_at, updated_at.

Индексы: `(status, is_searchable) WHERE deleted_at IS NULL` — отбор
индексируемых документов; `(category)` — фильтры в админке.

Отключаемые блоки знаний (как в Савви): `status`/`is_searchable` — документ
можно выключить из поиска, не удаляя [подтверждено владельцем].

## 14. `kb_chunks`

id, document_id FK, chunk_index int, content text, token_count int,
embedding `vector(1536)` (text-embedding-3-small), metadata jsonb (source,
category, language, valid_until, version, status, title, document_id,
chunk_index, is_searchable), embedding_model text, created_at.

Индексы:
- HNSW по `embedding` (`vector_cosine_ops`) — датасет маленький (сотни
  чанков), HNSW даёт стабильный recall без подбора списков IVFFlat;
  строится быстро.
- `UNIQUE (document_id, chunk_index)` — переиндексация идемпотентна:
  повторный прогон не плодит дубликаты чанков.

## 15. FAQ **[РАСШИРЕНИЕ — трёхъязычная политика, РЕШЕНИЕ владельца]**

### 15.1 `kb_faq`

| Поле | Тип | Примечание |
|---|---|---|
| id | bigint PK | |
| normalized_key | text NOT NULL | машинное имя записи |
| category | text NULL | |
| answer_ru | text NULL | русский — источник истины |
| answer_ru_status | answer_status NOT NULL default `absent` | |
| answer_kk | text NULL | |
| answer_kk_status | answer_status NOT NULL default `absent` | |
| answer_en | text NULL | |
| answer_en_status | answer_status NOT NULL default `absent` | |
| is_active | bool NOT NULL default false | |
| created_at / updated_at | timestamptz | |

`answer_status`: `absent` / `machine` / `approved`. `machine` — черновик,
клиенту НЕ отдаётся ни при каких условиях, виден только в админке.

Constraints:
- `UNIQUE (normalized_key)` — записи адресуемы, импорт/экспорт идемпотентен.
- `CHECK (NOT is_active OR answer_ru_status = 'approved')` — активная FAQ
  обязана иметь утверждённый русский ответ.
- `CHECK`-и вида `answer_kk_status = 'absent' OR answer_kk IS NOT NULL` —
  статус не может врать о наличии текста.

Правило отдачи (код, не схема): ответ на языке L отдаётся только при
`answer_L_status='approved'`; иначе — НЕ подставлять русский, а уйти в
RAG+LLM. Массовое утверждение переводов запрещено (только по одной записи).

Таблица создаётся ПУСТОЙ — никакого наполнения в миграциях/seed (SPEC §11).

### 15.2 `kb_faq_patterns`

id, faq_id FK, language (`ru`/`kk`/`en`), pattern text (как ввёл владелец),
normalized_pattern text (lowercase, без пунктуации/эмодзи), created_at.

Индексы:
- `UNIQUE (language, normalized_pattern)` — один и тот же вопрос не может
  вести к двум разным FAQ; конфликт виден при сохранении, а не в проде
  недетерминированным ответом.
- `(faq_id)` — рендер записи в админке.

Для каждой FAQ обязательны паттерны ru и kk (валидация при активации),
en — опционально.

### 15.3 `kb_faq_misses` **[РАСШИРЕНИЕ]**

Аналитика «FAQ-промахи по языку»: запрос совпал с паттерном (или ушёл в LLM),
но approved-ответа на языке клиента не было.

id, faq_id FK NULL (NULL = паттерн не совпал вовсе), language, category text
NULL, chat_id FK, created_at. Текст запроса НЕ хранится (PII).

Индекс `(language, created_at)` — отчёт за период; `(faq_id)` — топ тем.

## 16. `kb_processing_log`

id, document_id FK, step, status, detail, tokens_used, created_at.
Индекс `(document_id, created_at)`.

## 17. Деньги

### 17.1 `ai_calls`

id, correlation_id, chat_id FK NULL, message_id FK NULL, call_type
(`chat`/`transcription`/`embedding`/`vision`), model, input_tokens,
cached_input_tokens, output_tokens, reasoning_tokens, audio_seconds,
cost_usd numeric(12,8), cost_kzt numeric(12,4), latency_ms int,
rag_chunk_ids jsonb, cache_hit bool, reasoning_effort text,
instruction_version int NULL (какая версия инструкции отвечала — для
«почему так»), status, error_code, created_at. `chat_id NULL` = вызов
из песочницы: реальные деньги учитываются и без чата.

`cost_kzt` фиксируется по курсу на момент вызова — смена курса не
переписывает исторические цифры (обязательный тест SPEC §17).

Индексы: `(created_at)` — графики расходов; `(chat_id)` — стоимость диалога;
`(correlation_id)` — трассировка; `(model, created_at)` — разбивка по моделям.

### 17.2 `model_prices`

id, model, unit (`per_1m_input`/`per_1m_cached_input`/`per_1m_output`/
`per_minute_audio`), price_usd numeric(12,6), effective_from timestamptz,
created_by FK NULL.

Индекс `UNIQUE (model, unit, effective_from)` — на один момент времени одна
цена; расчёт берёт последнюю `effective_from <= момент вызова`.

### 17.3 `fx_rates`

id, pair (`USD/KZT`/`RUB/KZT`), rate numeric(12,4), effective_from,
changed_by FK, created_at. Курс задаёт владелец в админке, автоматической
загрузки из интернета нет (SPEC §9.1).

Индекс `UNIQUE (pair, effective_from)` — журнал курсов без двусмысленности,
каким курсом считался период.

### 17.4 `budget_ledger`

id, scope (`global_day`/`global_month`/`chat`/`user_day`), scope_key text
(`2026-08-31`, `chat:123`, `user:+77011234567:2026-08-31`), reserved_usd,
spent_usd, limit_usd (numeric(12,6)), period_start date, period_end date,
updated_at.

`UNIQUE (scope, scope_key, period_start)` — **сердце контроля расходов**:
резерв делается одним атомарным UPDATE по этой строке с условием
`reserved + spent + estimate <= limit` (SPEC §9.2). Уникальность гарантирует,
что у скоупа ровно одна строка-счётчик и все параллельные запросы
сериализуются на ней средствами PostgreSQL, а не приложением.

### 17.5 `budget_reservations`

id, ledger_id FK, correlation_id, amount_usd, status
(`held`/`settled`/`released`), created_at, settled_at.
correlation_id — суб-корреляция вида `{correlation}:main:a{attempt}`:
несколько платных вызовов и повторные попытки одного сообщения не
конфликтуют по UNIQUE; released-резерв терминален, новая попытка берёт
новый.

`UNIQUE (correlation_id, ledger_id)` — идемпотентность settle/release:
ретрай той же операции не спишет деньги дважды. Индекс
`(status, created_at) WHERE status='held'` — планировщик подчищает зависшие
резервы старше N минут.

## 17a. `media_jobs` **[РАСШИРЕНИЕ, фаза 7]**

id, message_id FK **UNIQUE**, chat_id FK, media_type, state
(`pending`/`processing`/`done`/`refused`/`failed`), attempts int,
lease_until, next_attempt_at, download_url text, mime_type, file_bytes,
duration_seconds, sha256, error_code, created_at, updated_at.

Зачем отдельная таблица, а не поля в `messages`: у медиа-обработки
СВОЙ счётчик попыток и свой лиз. Общий `messages.retry_count` уже
принадлежит AI-пайплайну (и вшит в sub-correlation резерва), поэтому
голосовое, потратившее 2 попытки на транскрибацию, приходило бы к
вызову модели без права на ретрай.

`UNIQUE (message_id)` — повторная доставка уведомления или двойная
постановка не создают вторую платную единицу работы.
Индексы: частичные по `next_attempt_at` и `chat_id` для
`state IN ('pending','processing')` — выборка воркера и проверка
«чат ещё ждёт медиа» (она же держит порядок сообщений в чате).

Плюс к `ai_calls` добавлен частичный UNIQUE
`(correlation_id, call_type) WHERE call_type IN ('transcription','vision')`
— барьер против повторной оплаты одного файла: строка вставляется ДО
платного вызова, и ретрай после краха упирается в констрейнт.

## 18. Конфигурация и системные

### 18.1 `settings`

key text PK, value jsonb, value_type, updated_by FK NULL, updated_at.
Все лимиты, модели, промпты-шаблоны, пороги, `alert_phone`, `org_name`,
`timezone`, `working_hours` (выключено по умолчанию — бот 24/7 [РЕШЕНИЕ]) —
живут здесь; `.env` задаёт стартовые значения и секреты.

### 18.2 `agent_instructions`

id, version int, system_prompt text, style text, allowed_topics jsonb,
forbidden_topics jsonb, fallback_templates jsonb, escalation_rules jsonb,
max_output_tokens int, model text, reasoning_effort text, is_active bool,
author_user_id FK, created_at.

Индексы: `UNIQUE (version)`; частичный `UNIQUE (is_active) WHERE is_active`
— активная версия ровно одна, гонка двух активаций разрешается БД.

### 18.3 `semantic_cache`

id, normalized_query_hash text, language, category text NOT NULL
default '' (пустая строка вместо NULL — иначе UNIQUE-ключ не
дедуплицирует), kb_version int,
answer text, hit_count int, created_at, expires_at.

`UNIQUE (normalized_query_hash, language, category, kb_version)` — ключ кэша
(SPEC §11); включение kb_version в ключ = инвалидация при изменении базы
знаний без сканирования. Индекс `(expires_at)` — чистка TTL.
Не кэшируются: персональные данные, запись/бронь, индивидуальные условия.

### 18.4 `system_events`

id, level (`info`/`warning`/`error`/`critical`), component, event,
detail jsonb, correlation_id, created_at.
Индексы `(created_at)`, `(level, created_at)` — страница статуса и алерты.

### 18.5 `alert_notifications` **[РАСШИРЕНИЕ]**

«Один раз на порог, без спама» (SPEC §9.3) требует памяти о том, что порог
уже отправлялся. Алерты уходят в WhatsApp на `settings.alert_phone` [РЕШЕНИЕ].

id, kind (`budget_day`/`budget_month`/`system`), event_key text
(`80pct`, `circuit_breaker`, ...), period_key text (`2026-08-31`, `2026-08`,
или пусто для событийных), sent_at, status (`sent`/`failed`/`pending`),
detail jsonb, created_at.

`UNIQUE (kind, event_key, period_key)` — повторная проверка порога в тот же
день/месяц не рождает второй алерт.

## 19. Retention (сводка; политика — в security-and-cost-controls.md)

| Данные | Срок |
|---|---|
| медиа-файлы | `MEDIA_RETENTION_DAYS`, дефолт 7 → автоудаление; транскрипты/описания остаются |
| `inbound_notifications.raw_payload` | обнуляется через 30 дней после обработки |
| сообщения, чаты, `ai_calls` | бессрочно (настраиваемо в будущем) |
| `admin_sessions` истёкшие | удаляются через 30 дней |
| `system_events` | 90 дней |
| `kb_faq_misses` | 180 дней |
| `audit_log` | 365 дней |

## 20. ENUM'ы

- `chat_mode`: `AUTO_AI`, `HUMAN`, `PAUSED_UNTIL`, `DISABLED`
- `msg_state`: `RECEIVED`, `QUEUED`, `PROCESSING`, `GENERATED`, `SENT`,
  `ANSWERED` (терминал ВХОДЯЩЕГО после ответа; добавлено в фазе 6 —
  `SENT` остался терминалом исходящих), `FAILED_RETRYABLE`,
  `FAILED_FINAL`, `IGNORED`, `HANDED_TO_HUMAN`, `BLOCKED_BY_LIMIT`
- `msg_source`: `customer`, `ai`, `operator_panel`, `operator_phone`, `system`
- `media_type`: `voice`, `image`, `video`, `gif`, `sticker`, `document`, `other`
- `answer_status`: `absent`, `machine`, `approved`
- `attention_reason`: `handoff`, `processing_error`, `blocked_by_limit`, `manual`
- `budget_scope`: `global_day`, `global_month`, `chat`, `user_day`
- `call_type`: `chat`, `transcription`, `embedding`, `vision`
- `language`: `ru`, `kk`, `en` (в `chat_memory` дополнительно `mixed`)

## 21. Сводка уникальных индексов и их обоснований

| Constraint | Что защищает |
|---|---|
| `inbound_notifications (instance_id, provider_message_id)` | повторная генерация/двойной вызов OpenAI на одно уведомление |
| `messages (chat_id, provider_message_id)` частичный | дубль входящего сообщения |
| `messages (outbound_message_id)` частичный | двойная отправка при ретрае |
| `chats (instance_id, wa_chat_id)` | два чата на одного собеседника при гонке |
| `budget_ledger (scope, scope_key, period_start)` | атомарность лимитов — одна строка-счётчик |
| `budget_reservations (correlation_id, ledger_id)` | двойной settle/release |
| `kb_faq (normalized_key)` | идемпотентный импорт/экспорт |
| `kb_faq_patterns (language, normalized_pattern)` | один вопрос → два разных ответа |
| `kb_chunks (document_id, chunk_index)` | дубли чанков при переиндексации |
| `semantic_cache (hash, language, category, kb_version)` | детерминированный ключ кэша + инвалидация |
| `agent_instructions (is_active) WHERE is_active` | две активные инструкции одновременно |
| `alert_notifications (kind, event_key, period_key)` | спам одинаковыми алертами |
| `model_prices (model, unit, effective_from)` | неоднозначность цены на момент вызова |
| `fx_rates (pair, effective_from)` | неоднозначность курса периода |
| `admin_user (phone)` | двойная учётка на один логин |
| `admin_sessions (session_token_hash)` | коллизия/подмена сессии |
| `tags (lower(name))` | дубли тегов в разном регистре |
