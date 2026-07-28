# 08 — Operational playbook: что делать когда что-то сломалось

## TL;DR

В этом файле — три категории материала: **smoke-тесты** (как
запустить и что значит результат), **morning_report** (как читать
ежедневный дайджест), и **топ gotchas** из реальных продакшн-инцидентов
2026 года. Каждый gotcha имеет симптом, диагноз и фикс.

## Smoke-тесты

Все smoke-тесты используют **изолированную временную БД** (tmp
sqlite через `tempfile`), не трогают `data/news_memory.db`. По
окончании удаляются.

```bash
sudo -u <deploy-user> bash -lc '
  cd /opt/<deploy-user>/media-fabrique-template
  for t in test_*_smoke.py; do
    echo "=== $t ==="
    .venv/bin/python "$t" 2>&1 | tail -5
  done
'
```

| Тест | Что проверяет | Если fail |
|---|---|---|
| `test_sprint_y_e2e_smoke.py` | весь конвейер end-to-end: fetch → rewrite → publish → generate_for_tg → publish_tg | обычно LLM API key или WP credentials |
| `test_main_orchestrator_smoke.py` | `main.py:TICK_REGISTRY` + subprocess.run() | см. gotcha ниже |
| `test_rewrite_and_score_smoke.py` | LLM JSON parse + Pydantic + scoring | устаревший формат промпта |
| `test_rewrite_and_score_scoring_smoke.py` | `score_item`, `decay`, `compute_expires_at` | чистая логика, падает только при неверной математике |
| `test_publisher_smoke.py` | WP REST API + image upload + post creation | WP credentials / network |
| `test_janitor_smoke.py` | DELETE expired, heal stuck | см. janitor gotcha |
| `test_tg_channel_publisher_smoke.py` | TG Bot API + Telegraph | TELEGRAM_BOT_TOKEN / TELEGRA_PH_ACCESS_TOKEN |
| `test_feedback_receiver_smoke.py` | webhook handler + `/approve /reject /edit` | OPENCLAW_TG_INGRESS (proxy) |
| `test_feedback_receiver_tg_smoke.py` | `/approve_tg /feedback_tg` | TG_CHANNEL_ID |
| `test_image_pipeline_smoke.py` | WebP encode (Plan A) | Pillow отсутствует в venv |
| `test_models_whitelist_smoke.py` | CATEGORY_WHITELIST синхронизирован с SQLite CHECK | новая категория добавлена в Python, но CHECK не обновлён |
| `test_morning_report_smoke.py` | morning_report без TG push (`--dry`) | обычно стабилен |
| `test_tg_bridge_curl_smoke.py` | curl из webhook receiver к OpenClaw ingress | если OpenClaw лежит |
| `test_tg_bridge_smoke.py` | tg_bridge.push_* функции | TELEGRAM_BOT_TOKEN |
| `test_scoring.py` | unit-тесты scoring | чистая логика |

**Sprint Y end-to-end smoke** должен показать **`19/19 PASS`**.
Это гарантия, что после деплоя конвейер работает в полном объёме.

## Post-deploy e2e (skill)

После sprint с деплоем — прогнать `tick=fetch/rewrite/publish/janitor`
на VPS и записать цифры в `MEMORY.md` (оператора). Этот процесс
автоматизирован через skill `post-deploy-e2e-smoke` — операторская
инструкция, не код шаблона.

Минимальный e2e руками:

```bash
sudo -u <deploy-user> bash -lc '
  cd /opt/<deploy-user>/media-fabrique-template
  for t in fetch rewrite publish generate_for_tg publish_tg janitor; do
    echo "=== tick=$t ==="
    .venv/bin/python main.py tick=$t 2>&1 | tail -3
  done
'
# Затем проверить лог heartbeat:
cat /opt/<deploy-user>/media-fabrique-template/logs/.last_tick | jq .
```

## Morning report

`morning_report.py` запускается ежедневно в 07:00 Europe/Moscow
через `mf-morning-report.sh` (см. `deploy/mf-morning-report.sh`).
Пуш в TG-топик `#morning-report` (thread_id задаётся через
`TG_THREAD_MORNING_REPORT`).

### Структура сообщения

```html
📅 <b>Morning report</b> · last 24h

📊 <b>Posts</b>
  · published: N
  · failed: M
  · skipped: K
  · ready: R (queued for next publish tick)
  · rewriting: Q (in-flight)
  <i>by category (published):</i>
    tech: 12
    politics: 8
    ai: 5
    ...

🚫 <b>Skipped / Failed</b>
  · skipped.violent: 3
  · skipped.political: 5
  · skipped.inoagent: 1
  <i>draft_posts failed:</i>
    telegraph_required_but_unavailable: 2
    wp_5xx: 1

🧠 <b>LLM usage</b>
  · runs: 45 (avg 4200 ms)
  · prompt: 320,000 tok · completion: 180,000 tok · thinking: 90,000 tok
  · ≈ $0.85 USD
```

### Что означают цифры

| Поле | Норма | Аномалия |
|---|---|---|
| `published` | 5-30/день для активного канала | 0 → fetch или publish не работает |
| `failed` | 0-2 | >5 → проверить Telegraph API, WP credentials |
| `skipped.violent` | 0-3 (зависит от темы) | резкий рост → LLM дрейфует, проверь `master_prompt.md` стоп-лист |
| `runs` | ~число published × 3-4 (rewrite + scoring + tg-gen) | 0 → LLM API down |
| `avg duration_ms` | 2000-6000 ms | >10000 → провайдер тормозит, см. их статус |
| `usd` | зависит от объёма и прайсинга | аномальный скачок → LLM вернул огромный JSON, проверить `thinking_tokens` |

## Топ-5 gotchas из реальных продакшн-инцидентов

### 1. `LANG=C.UTF-8` для cron (русские буквы в TG)

**Симптом**: TG-сообщения с кириллицей приходят как `??????` или
`UnicodeDecodeError: 'ascii' codec can't decode byte 0xd0`.

**Причина**: cron запускает процессы с POSIX locale (`LANG=`, `LC_ALL=`).
`open(...)` + UTF-8 не работает, потому что Python locale определяется
по env, а env от cron пустой.

**Фикс**: в `/etc/cron.d/<project>-pipeline` (и в systemd-юните)
обязательно:

```cron
LANG=C.UTF-8
```

или в systemd:

```ini
Environment=LANG=C.UTF-8
```

Без этого TG-сообщения бьются, Telegram API возвращает 400
"can't parse entities" на русском тексте.

### 2. Telegraph IPv6 мёртв, urllib переключён на IPv4

**Симптом**: `tick=publish_tg` молча падает с
`TelegraphUnavailable after 3 attempts`. Логи показывают
`URLError: <urlopen error timed out>` на `api.telegra.ph`.

**Причина**: `api.telegra.ph` имеет IPv4 + IPv6, но у части VPS
IPv6-стек отвечает очень медленно или вообще не маршрутизируется.
Tenacity retry упирается в таймаут.

**История фиксов**:
- Sprint 6d.7 (июль 2026): `curl --ipv4` — починило.
- Sprint X hotfix (сентябрь 2026): curl стал таймаутить,
  urllib — нет. Переключились обратно на urllib.

**Что делать сейчас**: ничего, urllib работает happy-path. Если
завтра опять Telegraph timeout — проверить `getent hosts api.telegra.ph`,
попробовать `curl -4 https://api.telegra.ph/createPage` вручную,
вернуться к `curl --ipv4` (соответствующий код закомментирован в
`telegraph.py:_http_post` docstring).

### 3. Telegram webhook last-write-wins — не запускать 2 инстанса

**Симптом**: `/approve <id>` срабатывает дважды, в TG-канал
улетают два `sendMessage` для одного поста.

**Причина**: TG webhook "last-write-wins" — если зарегистрировано
два webhook URL (например, тестовый на другом сервере), TG шлёт
update в один, но **не гарантирует** exactly-one доставку для
обоих. Если запустить два systemd-инстанса `telegram-receiver`,
оба получат update.

**Фикс**: убедиться, что работает **ровно один** инстанс receiver'а:

```bash
sudo systemctl status <project>-telegram-receiver
# → Active: active (running) — ОДИН процесс

ps aux | grep telegram_receiver | grep -v grep
# → ОДНА строка
```

Если видно два процесса — kill второй. Идемпотентность спасает
от дубля `approved` (UPDATE rowcount==0), но `sendMessage` не
идемпотентен без `message_id` — TG примет оба и опубликует в
канал дважды.

### 4. WP Application Passwords с пробелами — env quoting

**Симптом**: WP REST возвращает 401 "неверные credentials" для
валидного Application Password.

**Причина**: WP Application Password имеет формат
`xxxx xxxx xxxx xxxx xxxx xxxx` (4 группы по 4 символа с пробелами).
Если shell обрезает значение по пробелу — теряется половина
пароля. Cron не делает shell-quoting для значений env, потому что
cron не запускает shell.

**Фикс**: в `/etc/cron.d/<project>-pipeline` значение WP_APP_PASSWORD
берётся **из** `/opt/<deploy-user>/.env` через `EnvironmentFile` в
systemd-юните (не из cron!). В cron `Environment=` с пробелами
не работает.

Для systemd-юнита (правильный путь для всех внешних вызовов WP):

```ini
[Service]
EnvironmentFile=/opt/<deploy-user>/.env
```

И в `.env` использовать **одинарные кавычки** вокруг значения:

```ini
WP_APP_PASSWORD='xxxx xxxx xxxx xxxx xxxx xxxx'
```

Хотя `dotenv` парсит оба варианта — одинарные кавычки надёжнее
на случай если shell где-то интерполирует.

### 5. SQLite single-writer — concurrent write → SQLITE_BUSY

**Симптом**: при частом cron'е `tick=fetch` и `tick=rewrite`
одновременно один из них завершается с `sqlite3.OperationalError: database is locked`.

**Причина**: SQLite — single-writer. WAL mode допускает concurrent
readers + 1 writer, но **два** writer'а сериализуются, и второй
получает SQLITE_BUSY если транзакция первого длиннее busy_timeout.

**Фикс**:

1. По умолчанию `busy_timeout=5s` (sqlite3 default). Если конкуренция
   частая — увеличить в `models.py:_connect()`:

```python
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA busy_timeout=30000;")   # 30 секунд
```

2. **Дешевле**: развести cron-тики по времени. Например,
   `tick=rewrite` сдвинуть на `*/7`, `tick=fetch` на `*/30`
   (уже сделано) — перекрытие минимальное.

3. **Самый дешёвый**: cron запускает тики последовательно, каждая
   транзакция SQLite занимает миллисекунды (один UPDATE + COMMIT),
   busy_timeout=5s **более чем достаточно**. Проблема возникает
   только если долбить одну таблицу из двух тиков в одну секунду.

Если всё-таки SQLITE_BUSY появится — `python -m migrate.py` имеет
миграцию, которая **не** падает на busy (retry внутри).

## Миграции (кратко)

`migrate.py` — идемпотентный runner миграций:

```bash
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/python \
  /opt/<deploy-user>/media-fabrique-template/migrate.py
```

Каждая миграция — функция `migration_NNN_short_name(conn)` +
запись в `_migrations` таблицу. Перезапуск = no-op.

Создание новой миграции:

```python
# migrate.py
def migration_023_add_my_column(conn):
    """Add candidates.foo column."""
    if conn.execute("PRAGMA table_info(candidates)").fetchall() and \
       'foo' not in [r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()]:
        conn.execute("ALTER TABLE candidates ADD COLUMN foo TEXT")
        conn.commit()
```

Пересоздание таблицы для смены CHECK constraint (см.
[`04-categorization.md`](04-categorization.md) шаг 2) — стандартный
SQLite-паттерн CREATE-новой → INSERT-SELECT → DROP → RENAME.

## Backup recovery

```bash
# Из локального backup
sudo systemctl stop <project>-telegram-receiver
sudo cp /var/backups/<deploy-user>/news_memory_latest.db \
       /opt/<deploy-user>/media-fabrique-template/data/news_memory.db
sudo chown <deploy-user>:<deploy-user> /opt/<deploy-user>/media-fabrique-template/data/news_memory.db
sudo systemctl start <project>-telegram-receiver

# Проверка
sudo -u <deploy-user> sqlite3 /opt/<deploy-user>/media-fabrique-template/data/news_memory.db \
  "SELECT COUNT(*), status FROM candidates GROUP BY status;"
```

## Где спросить

- **GitHub Issues**: <https://github.com/DimbikeY/media-fabrique-template/issues>
  — обсуждение дизайна, bug reports, feature requests.
- Без SLA. Автор отвечает по мере сил, **без гарантий** времени
  реакции.
- Этот шаблон **не** предоставляет коммерческую поддержку.
  Используйте на свой страх и риск.

## Где форкать

| Что мониторить | Файл / место |
|---|---|
| Heartbeat | `logs/.last_tick` (после каждого тика) |
| Daily summary | TG `#morning-report` topic |
| Per-tick exit code | `journalctl -u <project>-telegram-receiver` (receiver) + `/var/log/<project>/*.log` (тики) |
| Добавить новый smoke | `test_<name>_smoke.py` рядом с тестируемым модулем |
| Кастомные gotchas | `scripts/security_audit.sh` (расширить checks) |

## Следующее

- [`00-overview.md`](00-overview.md) — общая карта.
- [`01-pipeline.md`](01-pipeline.md) — что делает каждый тик.
- [`07-self-host.md`](07-self-host.md) — развёртывание с нуля.
