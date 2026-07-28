# 03 — Архитектура расписания: cron + systemd

## TL;DR

Конвейер оркеструется **system cron** (`/etc/cron.d/<project>-pipeline`),
а единственный long-running процесс — Telegram webhook receiver —
живёт под **systemd** (`<project>-telegram-receiver.service`). Никакого
Airflow, k8s или systemd-timer'ов поверх cron'а — это оверкилл для
30 постов/день на одном VPS.

## Расписание

Все тики определены в одном месте — `config.py:PIPE_TICKS`:

```python
@dataclass
class PipelineTicksConfig:
    rewriter_limit: int = _env_int("TICK_REWRITER_LIMIT", 5)
    publisher_limit: int = _env_int("TICK_PUBLISHER_LIMIT", 3)
    fetcher_max: int    = _env_int("TICK_FETCHER_MAX", 50)

    fetcher_cron:         str = _env("TICK_FETCHER_CRON",         "*/30 * * * *")
    rewriter_cron:        str = _env("TICK_REWRITER_CRON",        "*/7  * * * *")
    publisher_cron:       str = _env("TICK_PUBLISHER_CRON",       "*/5  * * * *")
    janitor_cron:         str = _env("TICK_JANITOR_CRON",         "0    * * * *")
    generate_for_tg_cron: str = _env("TICK_GENERATE_FOR_TG_CRON", "*/5  * * * *")
    publish_tg_cron:      str = _env("TICK_PUBLISH_TG_CRON",      "*/10 * * * *")
```

Каждой cron-строке соответствует `python main.py tick=<name>` через
`/etc/cron.d/<project>-pipeline`:

```cron
# /etc/cron.d/<project>-pipeline
SHELL=/bin/bash
PATH=/opt/<deploy-user>/.venv/bin:/usr/local/bin:/usr/bin:/bin
PYTHONUNBUFFERED=1
LANG=C.UTF-8   # важно для cron: TG-сообщения с кириллицей

*/30 * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=fetch         >> /var/log/<project>/fetch.log    2>&1
*/7  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=rewrite       >> /var/log/<project>/rewrite.log  2>&1
*/5  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=publish       >> /var/log/<project>/publisher.log 2>&1
*/5  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=generate_for_tg >> /var/log/<project>/gen_tg.log    2>&1
*/10 * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=publish_tg     >> /var/log/<project>/publish_tg.log 2>&1
0    * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=janitor       >> /var/log/<project>/janitor.log  2>&1

# Morning report — отдельный скрипт-обёртка (см. deploy/mf-morning-report.sh)
0  7 * * *   <deploy-user>  /opt/<deploy-user>-ops/bin/mf-morning-report.sh >> /var/log/<project>/mf-morning.log 2>&1
```

**`LANG=C.UTF-8`** — критично. Без него cron стартует с POSIX locale,
русский текст в TG-сообщениях превращается в `?????` или
`UnicodeDecodeError: 'ascii' codec can't decode`. См.
[`08-operational-playbook.md`](08-operational-playbook.md) gotcha #1.

### Что и почему

| Tick | Cron | Почему такой интервал |
|---|---|---|
| `tick=fetch` | `*/30` | RSS обновляются медленно (5–60 мин); чаще = больше мусора в `candidates` |
| `tick=rewrite` | `*/7` | LLM-вызов — самый дорогой; */7 даёт ~8 пересказов/час с хорошим cadence |
| `tick=publish` | `*/5` | Очередь WP растёт быстро (rewrite готов → publish); */5 минимизирует dwell time |
| `tick=generate_for_tg` | `*/5` | После WP-публикации нужно сгенерировать TG-preview; */5 синхронизирован с publish |
| `tick=publish_tg` | `*/10` | TG публикация дороже (Telegraph retry); реже → меньше Telegraph rate-limit |
| `tick=janitor` | `0 * * * *` | Час — типичный half-life для mid-tier категорий (tech/business = 24 ч) |
| morning report | `0 7 * * *` | 07:00 Europe/Moscow = локальное утро оператора |

## systemd unit — long-running webhook receiver

`/etc/systemd/system/<project>-telegram-receiver.service`:

```ini
[Unit]
Description=<project> Telegram webhook receiver
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<deploy-user>
Group=<deploy-user>
WorkingDirectory=/opt/<deploy-user>/media-fabrique-template

EnvironmentFile=/opt/<deploy-user>/.env
Environment=PYTHONUNBUFFERED=1
Environment=LANG=C.UTF-8

ExecStart=/opt/<deploy-user>/.venv/bin/python -m telegram_receiver
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

# Hardening (Sprint 6m security audit, секция M)
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/<deploy-user>/media-fabrique-template/data /var/log/<project>

[Install]
WantedBy=multi-user.target
```

`Type=simple` потому что `telegram_receiver.serve()` запускает
`ThreadingHTTPServer` синхронно в main-потоке; systemd увидит
"started" после успешного `bind()`. `Restart=on-failure` плюс
`RestartSec=5` — если TG API подвиснет и процесс крэшнётся,
systemd поднимет его за 5 секунд.

`LISTEN_HOST = "127.0.0.1"`, `LISTEN_PORT = 8788` — listener
принимает только с localhost, в мир смотрит через nginx reverse proxy
(см. [`07-self-host.md`](07-self-host.md) секция про nginx).

## Backoff и jitter в каждом тике

Cron сам по себе **не делает backoff**. Если RSS-фид вернул 5xx
три раза подряд, tenacity в `rss_fetcher.fetch_feed()`:

```python
@retry(
    reraise=True,
    stop=stop_after_attempt(PIPE.http_retries),                # 3
    wait=wait_exponential(multiplier=PIPE.http_retry_backoff_seconds, min=1, max=10),
    retry=retry_if_exception_type((requests.RequestException,)),
)
def fetch_feed(url: str) -> feedparser.FeedParserDict:
    ...
```

То есть `~1.5s → ~3s → ~6s` (max 10s) перед тем, как сдаться.
Аналогично в `telegraph._http_post_with_retry`:
`1s → 2s → 4s`. Дальше тик завершается, cron отметит exit code,
`morning_report` покажет ошибку в #morning-report, оператор
смотрит лог.

**Jitter** для cron не нужен: все тики идемпотентны и пишут в
SQLite (single-writer), конкуренция за ресурсы исключена. Если в
будущем появится реальная конкуренция (например, несколько
`tick=rewrite` параллельно) — добавим `random delay` через
`flock(1)` lockfile, но сейчас это overengineering.

## Log rotation

Все cron-логи пишутся в `/var/log/<project>/*.log` через `>>`,
поэтому logrotate обязателен:

```bash
# /etc/logrotate.d/<project>
/var/log/<project>/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 <deploy-user> <deploy-user>
    sharedscripts
    postrotate
        systemctl reload <project>-telegram-receiver.service >/dev/null 2>&1 || true
    endscript
}
```

14 дней retention. `delaycompress` — вчерашний лог не компрессится
сразу (полезно при активной отладке). `sharedscripts` +
`postrotate` — единый reload systemd-юнита, не пофайлово.

`logs/.last_tick` (heartbeat JSON) — **не** ротируется logrotate'ом,
это одна строка на тик; но `main.py` каждый раз переписывает его
атомарно через `tmp + rename`, так что ротация не нужна.

## Почему cron, не k8s/Airflow

| Критерий | cron | k8s / Airflow |
|---|---|---|
| 30 постов/день × 5 тиков = ~150 job'ов в сутки | нативно | нужен cluster, scheduler, persistent volume |
| Crash recovery | systemd `Restart=on-failure` для receiver, для cron — следующий тик | да, но поверх etcd / Zookeeper |
| UI для мониторинга | `cat logs/.last_tick` + systemd status | нужен Prometheus + Grafana |
| Цена | 0 (уже в дистрибутиве) | minikube=free, prod=$$$ |
| Сложность деплоя | `apt install cron` | kubectl, helm, ingress, secrets |
| Один VPS | нативно | anti-pattern (k8s на 1 ноде — это просто docker-compose с лишним слоем) |

Cron **проигрывает** в трёх случаях: (1) больше одного VPS'а с
общим state, (2) динамическое масштабирование, (3) сложные DAG с
ветвлением/зависимостями. Media Fabrique — single VPS, фиксированное
расписание, всё линейно. Cron идеален.

## Где форкать

| Что поменять | Файл / место |
|---|---|
| Cron-расписание | `config.py:PIPE_TICKS` + `/etc/cron.d/<project>-pipeline` |
| Per-tick batch size | `config.py:PIPE_TICKS.<role>_limit` или `--limit N` CLI |
| HTTP retry policy | `config.py:PipelineConfig.http_*` |
| Добавить новый тик | `main.py:TICK_REGISTRY` + новый standalone скрипт |
| systemd unit | `/etc/systemd/system/<project>-telegram-receiver.service` |
| logrotate | `/etc/logrotate.d/<project>` |

## Следующее

- [`01-pipeline.md`](01-pipeline.md) — что делает каждый тик.
- [`07-self-host.md`](07-self-host.md) — пошаговый деплой systemd unit.
- [`08-operational-playbook.md`](08-operational-playbook.md) — cron gotchas (LANG, IPv6).
