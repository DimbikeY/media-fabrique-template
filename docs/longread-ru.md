
# Media Fabrique Template: как собрать самодельный новостной конвейер и почему это дешевле, чем кажется



---

## 1. Hook

Шесть утра. RSS-ридер открыт в соседней вкладке, фиды за ночь накопили 200+ непрочитанных заголовков. Среди них типично 2-3 важные новости, и ещё 5-6 потенциально интересные для канала. Остальные 190 — мусор, который не будет прочитан и не нужен.

Этот gap — между потоком RSS и тем, что обычно стоит внимания — и есть причины, по которым построен **Media Fabrique**. Это self-hosted пайплайн, который тащит RSS-фиды, отдаёт тексты в LLM на пересказ, оценивает их по half-life функции, генерирует картинку и публикует в WordPress и Telegram-канал. State — SQLite, оркестрация — system cron, бюджет на **разработку** — около $40 единоразово (настройка 2 GB VPS, ежегодная аренда домена, подписка MiniMax для разработки на первые месяцы). Бюджет на **поддержание** — **$24/мес в pay-as-you-go режиме** (рекомендуется для production, доступность 100%) или **$16/мес по подписке** MiniMax Starter (экономия ~$90/год, но лимиты могут кончиться и пайплайн встанет)   

Начат как эксперимент для новостного канала. Выделено переиспользуемое ядро — это уже шаблон, не production-сервис. Форкни, поменяй RSS-источники на свои, перепиши промпт под свои темы, укажи свой WordPress и Telegram — через час у тебя рабочий конвейер. Весь код открыт (MIT license), самопроверяется smoke-тестами, разворачивается одним скриптом.

Этот longread — полный разбор: зачем, как устроено, где грабли, что стоит переделать. Если интересуешься self-hosted автоматизацией на стыке LLM, RSS и организацией пайплайнов — поехали.

<div align="center">

![TG-пост в канале с Telegraph Instant View](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.44.52.png)

*Готовая публикация в Telegram-канале: Telegraph Instant View (⚡) — полная статья открывается одним нажатием, без Telegram Premium. Те же текст и картинка, что в WordPress, но быстрее и без ограничений.*

</div>

---

## 2. Что такое Media Fabrique

**Media Fabrique** — это не продукт, это шаблон. Та штука, которую ты форкаешь, адаптируешь под свои нужды и забываешь до следующего раза. Под капотом — рабочий пайплайн, который уже около месяца автоматически публикует 30+ постов в день в моём новостном канале на VPS за несколько $ в месяц.

**Что делает**:
- Ходит по RSS-фидам каждые 30 минут
- Отдаёт содержимое кандидата в LLM с инструкцией «оцени, перескажи, не пропусти спам»
- Скорит по half-life функции в разрезе категории (моложе = выше приоритет); новости AI устаревает быстрее, чем новости в мире культуры  
- Генерирует featured-картинку (300×200 px WebP)
- Публикует в WordPress через REST API
- Параллельно готовит Telegram-preview с Telegraph mirror для Instant View

**Что не делает**:
- Не пишет оригинальный контент (только пересказ)
- Не ведёт social media (только один TG-канал)
- Не имеет UI (только CLI + TG-бот для модерации)

**Цифры**:
- 12 RSS-фидов, ~700-1200 кандидатов в день
- LLM-фильтр отсеивает ~85%, ~150 пересказов в день
- Scoring выбирает top-30 по half-life
- 30 публикаций в канал + 30 постов в WordPress
- Новостной канал: [@the_vremya](https://t.me/the_vremya)
- VPS: 2 GB RAM, 1 vCPU, $8/мес
- LLM (pay-as-you-go, рекомендуется для production): **$15.48/мес** ($0.30/M input + $1.20/M output, ~22.8M tokens с учётом CoT)
- LLM (по подписка MiniMax Starter $20/мес): ~$8/мес эффективно, **но лимиты могут кончиться → конвейер встанет** (см. §6)
- Домен: ~$2/год
- Подписка MiniMax для разработки - $20 (отдельная, для OpenClaw, не для LLM-API пайплайна)
- **Итоговые периодические расходы (pay-as-you-go): ~$23.65/мес** — доступность 100%, без риска лимитов
- Итоговые периодические расходы (подписка): ~$16/мес — экономия ~$90/год, **но риск даунтайма**
- Итоговые единоразовые расходы: ~$22

- Итоговые расходы за первый месяц (pay-as-you-go): ~$46
- Итоговые расходы за последующие месяцы (pay-as-you-go): ~$24/мес

<div align="center">

![Morning report от 26 июля: 38 публикаций за 24ч, разбивка по категориям, $0.58 на LLM](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.47.17.png)

*Утренний отчёт в TG-группу модерации: сколько опубликовали, что пропустили, сколько потратили на LLM. Каждый день, автоматически, без скрытых метрик в Grafana.*

</div>

**Стек**:
- Python 3.11+ `.venv` (никаких venv-manager'ов)
- SQLite для состояния — миграции через `migrate.py`
- OpenAI Python SDK (openai>=1.40) — работает с любым OpenAI-совместимым эндпоинтом
- WordPress REST API + Application Passwords
- Telegram Bot API + Telegraph API
- Pillow (WebP encode)
- feedparser + readability-lxml (HTML normalization)
- System cron для оркестрации

---

## 3. Архитектура

Один конвейер = несколько cron-тиков. Каждый tick читает SQLite, делает одну точечную работу, обновляет состояние БД. Никакого центрального цикла, никакой шины событий, никакой очереди сообщений.

```
┌─────────────┐
│  RSS-фиды   │  12 источников, ~700-1200 items/day
└──────┬──────┘
       │ tick=fetch (каждые 30 мин)
       ▼
┌─────────────────────────────┐
│ candidates (SQLite)         │  ~1000 новых в день
│ status: new                 │
└──────┬──────────────────────┘
       │ tick=rewrite (каждые 7 мин)
       ▼
┌─────────────────────────────┐
│ LLM-пересказ + scoring      │  minimax-m3 (+ rollback на 2.7-highspeed), ~22.8M tokens/мес
│ - стоп-темы (N1, N2)        │
│ - категория из 12 (whitelist) │
│ - half-life score           │
└──────┬──────────────────────┘
       │ (для top-30 по score)
       ▼
┌─────────────────────────────┐
│ draft_posts (SQLite)        │  ~30 ready в день
│ status: ready               │
└──────┬──────────────────────┘
       │ tick=publish (каждые 5 мин)
       ▼
┌─────────────────────────────┐
│ WordPress REST API          │  status=publish (auto)
│ + featured image (WebP)     │
└─────────────────────────────┘

       │ (параллельная ветка)
       │
┌─────────────────────────────┐
│ tick=generate_for_tg        │  отдельный LLM-вызов с коротким prompt
│ → tg_dispatch.awaiting      │
└──────┬──────────────────────┘
       │ tick=publish_tg (каждые 10 мин)
       ▼
┌─────────────────────────────┐
│ Telegram channel publish    │  Telegraph mirror для IV
└─────────────────────────────┘
```

**Почему не Celery / Airflow / Temporal**:

- Один VPS, 30 постов/день, 2 GB RAM — overhead менеджера задач дороже, чем сами задачи
- Cron уже умеет расписание, retry (если настроить), alerting (через logwatch)
- Всего 6 тиков — это меньше, чем количество типов в Airflow
- Каждый tick идемпотентен — если он упадёт, следующий tick продолжит с того же места

Подробный разбор cron-архитектуры — в [`docs/architecture/03-cron-architecture.md`](docs/architecture/03-cron-architecture.md).

<div align="center">

![TG-бот ассистента: команды `/preview`, `/approve`, `/reject`, `/feedback` для draft_post 655](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.46.38.png)

*Каждый draft перед публикацией проходит через человека: бот шлёт превью в TG-группу модерации, человек жмёт `/preview` → читает → `/approve` (опубликовать) или `/reject` (отклонить). Авто-publish включается одной env-переменной, но по умолчанию — human-in-the-loop.*

</div>

---

## 4. State machine в SQLite

Все три ключевые сущности — `candidates`, `draft_posts`, `tg_dispatch` — это **конечные автоматы на SQL**. Никакого Redis, никаких блокировок, никакой модели акторов. Каждый переход между состояниями — это `UPDATE ... WHERE id=? AND status=?` с проверкой `cur.rowcount == 1`.

```sql
-- Атомарный claim: "МЫ" — единственный, кто переводит ready → publishing
UPDATE draft_posts
SET status = 'publishing', claimed_at = now()
WHERE id = ? AND status = 'ready';
-- Если rowcount == 1 — наш. Если 0 — кто-то другой уже забрал.
```



**Три таблицы, три конечных автомата**:

**`candidates`** (новый → пересказ → готово/пропущено/ошибка):
- `new` — только что из RSS
- `rewriting` — LLM сейчас работает над ним
- `ready` — пересказ есть, score есть, draft_posts row создан
- `skipped` — LLM сказал "blocked=true" (стоп-тема)
- `failed` — LLM упал / невалидный JSON

**`draft_posts`** (черновик → одобрен → опубликован, или отклонён):
- `ready` — есть LLM-пересказ, но image ещё нет
- `image_ready` — image готов
- `publishing` — `tick=publish` сейчас публикует в WP
- `published` — WP POST/PATCH ok, пост live
- `failed` — WP error
- `rejected` — отклонён (через `/reject` в TG-боте)

**`tg_dispatch`** (TG-preview → одобрен → опубликован, или отклонён):
- `pending_tg_text` — ждём `tick=generate_for_tg`
- `awaiting_approval` — preview отправлен в TG-группу модерации
- `approved` — `/approve_tg` нажат
- `published_tg` — TG channel publish ok
- `rejected_tg` — `/reject_tg` нажат

**Race conditions** — избегаются двумя способами:

1. **Атомарный UPDATE с rowcount==1** — каждый tick забирает только одну строку под обновлением
2. **`error_reason = 'worker:HOST:PID'`** — soft-marker, кто сейчас работает. Если обработчик умер, остаётся последний id обработчика. Полезно для forensic.

**Почему SQLite, а не Redis**:

- Один writer в SQLite — это и есть **lock**. Не нужен распределённый mutex.
- WAL mode: write не блокируют read.
- Единый источник правды в одном файле — `rsync` его как обычный файл для backup.
- Миграции через `migrate.py` — идемпотентные, track в `_migrations` таблице.

Когда SQLite не хватит: >100 записей/сек  

Подробный разбор — в [`docs/architecture/02-state-machine.md`](docs/architecture/02-state-machine.md).

---

## 5. Cron вместо оркестратора

**Расписание** (`/etc/cron.d/{project}-pipeline`):

**Формат строки**: `минуты часы день_месяца месяц день_недели пользователь команда`

- `*/30 * * * *` — каждый час в 0 и 30 минут → `tick=fetch` (RSS-фиды)
- `*/7  * * * *` — каждые 7 минут → `tick=rewrite` (LLM-пересказ)
- `*/5  * * * *` — каждые 5 минут → `tick=publish` (WordPress) и `tick=generate_for_tg` (TG preview)
- `*/10 * * * *` — каждые 10 минут → `tick=publish_tg` (Telegram channel)
- `0    * * * *` — каждый час в 0 минут → `tick=janitor` (delete expired, heal stuck)

**Запуск всех тиков** — от пользователя `{deploy-user}` из `/opt/{deploy-user}/media-fabrique-template`. Полная команда всегда выглядит как: пользователь `{deploy-user}` → `cd /opt/{deploy-user}/media-fabrique-template` → `.venv/bin/python main.py tick=<name>`.

**Что даёт cron по сравнению с Celery**:

- cron уже умеет: расписание, логи, ENVIRONMENT, user context
- cron проще мониторить: `grep cron /var/log/syslog` — доступны все тики к чтению  
- cron не падает целиком — упал один tick, остальные работают
- cron не требует отдельного обработчика — на VPS запускается та же `python main.py`

**Что cron не умеет** (и почему это OK):

- Нет очереди повторов — но каждый tick идемпотентен, поэтому повтор = просто следующий tick
- Нет приоритетов — все тики одинаковой важности
- Нет UI — но cron не требует UI, это просто cron

**Long-running webhook** — отдельный systemd unit:

```ini
# /etc/systemd/system/{project}-telegram-receiver.service
[Unit]
Description=Media Fabrique Telegram webhook receiver
After=network.target

[Service]
Type=simple
User={deploy-user}
WorkingDirectory=/opt/{deploy-user}/media-fabrique-template
ExecStart=/opt/{deploy-user}/.venv/bin/python telegram_receiver.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```



**Backoff, jitter, error handling** — в коде каждого tick (`main.py`):

```python
def run_tick(tick: str):
    for attempt in range(env.int("TICK_MAX_RETRIES", 3)):
        try:
            subprocess.run([sys.executable, "main.py", f"tick={tick}"], check=True)
            return
        except subprocess.CalledProcessError as e:
            if attempt < env.int("TICK_MAX_RETRIES", 3) - 1:
                sleep = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(sleep)
                log.warning(f"tick={tick} retry {attempt+1} after {sleep:.1f}s")
            else:
                log.error(f"tick={tick} failed after {attempt+1} attempts: {e}")
                notify_dd(e)
```



Каждый tick изолирован, не падает общий процесс. Ошибка в `tick=publish` не ломает `tick=fetch`.

Подробнее — в [`docs/architecture/03-cron-architecture.md`](docs/architecture/03-cron-architecture.md).

---

## 6. LLM-интеграция

Провайдер: minimax m3. Дешево, сердито, большие лимиты, низкая цена, нет санкционных ограничений.

Стоимость (актуальные тарифы MiniMax API, 2026):

| Параметр | Подписка MiniMax (Starter, $20/мес) | Pay-as-you-go (MiniMax API direct) |
|---|---|---|
| Input | включён в подписку | **$0.30 / 1M tokens** |
| Output | включён в подписку | **$1.20 / 1M tokens** |
| Cache read | — | $0.06 / 1M tokens |
| CoT (60-120 сек) | включён в лимиты подписки | тарифицируется как output |
| Blended rate | ~$0.35/M tokens | ~$0.65-0.75/M tokens |
| Итого на 22.8M tokens/мес | **~$8/мес фактически** | **~$15-17/мес фактически** |

Расчёт pay-as-you-go (с учётом CoT thinking tokens):
- 200 пересказов/день × 30 дней = 6000 пересказов/мес
- На каждый: 3000 input + 1400 output (800 видимый + 600 thinking) = 4400 tokens
- Input: 6000 × 3000 × $0.30/M = **$5.40/мес**
- Output: 6000 × 1400 × $1.20/M = **$10.08/мес**
- **Итого: ~$15.48/мес** (~$0.65/M в среднем). С реальным распределением по категориям ~$15-17/мес.

Расчёт подписки:
- MiniMax Starter ($20/мес) включает CoT и лимиты, эффективная стоимость для нашего объёма — ~$8/мес ($0.35/M в среднем). Без жёстких квот сверх подписки, лимиты сбрасываются каждые 5 часов, еженедельно и ежемесячно.

**⚠️ Подписка vs pay-as-you-go для production**:

Подписка дешевле на ~$7.5/мес (~$90/год), но у неё есть **критический минус для production** — лимиты могут закончиться, и конвейер встанет. Это значит:
- Внезапные даунтаймы пайплайна в час пик, без предупреждения
- Необходимость мониторинга расхода лимитов 24/7 (отдельная задача)
- Масштабирование болезненно: добавить 5 RSS-источников = +30-50% к расходу токенов → лимит кончается быстрее → очередь встанет
- CoT-приоритет ниже чем у pay-as-you-go клиентов — при наплыве их запросы обслуживаются первыми

Pay-as-you-go для production-пайплайна:
- **Доступность 100%** — нет лимитов, конвейер не встанет
- CoT-токены приоритезируются на том же уровне что и обычные
- Гибкое масштабирование — добавил RSS-источник, не думаешь о лимитах
- Необходимость заплатить больше ~$7.5/мес (~$90/год) — это стоимость предотвращения даунтайма

**Рекомендация**: для production-пайплайна с десятками постов в день pay-as-you-go — правильный выбор. Подписка — для экспериментов и прототипов с контролируемым объёмом.

**Расход токенов**: 22.8M/мес (рассчитано: 200 × 3800 × 30). Реальный blended с CoT — выше.

**Stop-темы** — JSON-флаг в ответе LLM:

```python
# В system prompt:
"Если новость про N1/N2/N3/N4 — верни {\"blocked\": true, \"reason\": \"N1\"}"

# В коде:
result = llm_client.complete(...)
if result.get("blocked"):
    candidates._mark_skipped(candidate_id, reason=result["reason"])
    return
```



**Categorization** — closed-set из 12 тем (Sprint 6.7):

```python
CATEGORIES_WHITELIST = [
    "tech", "business", "science", "health", "sport",
    "culture", "cinema", "gaming", "auto", "travel",
    "food", "misc"
]
```



LLM не классифицирует в runtime — это deterministic. Вместо этого prompt просит модель **выбрать одну категорию из списка**. Это:
- дешевле (нет лишних токенов на классификацию)
- стабильнее (нет "drift" между запусками)
- проще валидировать (легко grep плохие категории)

**Prompt version pinning** (`prompts.TG_PROMPT_VERSION`):

```python
# Masthead prompt version, например "v2.3"
# При изменении промпта — bump version
# Master prompt + TG prompt имеют отдельные версии
```



Если новый prompt работает хуже — откатываем через `prompts.TG_PROMPT_VERSION = "v2.2"` и фиксируем regression без переобучения.

**Tenacity retries**:

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def llm_complete(prompt: str, max_tokens: int = 2000) -> dict:
    return openai_client.chat.completions.create(
        model=os.environ["LLM_MODEL"],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
```



3 попытки, exponential backoff 1-10 сек. Если все 3 упали — candidate идёт в `failed`, janitor повторит через час.

Подробнее — в [`docs/architecture/04-categorization.md`](docs/architecture/04-categorization.md).

---

## 7. Telegram специфика

**Long-poll vs Webhook** — и почему webhook подходит лучше в текущих ограничениях.

Long-poll (то, что `python-telegram-bot` использует по умолчанию):
- Проще конфигурировать (не нужен публичный URL)
- Работает за NAT
- Но: держит HTTP-соединение постоянно, занимает слот обработчика, медленнее

Webhook:
- Требует публичный URL + TLS
- Telegram сам шлёт HTTP POST на эндпоинт
- Быстрее, не занимает обработчик
- Регистрируется через `setWebhook` API

Telegraph + Instant View — замена Telegram Premium в контексте богатства опций редактирования  

```python
# В tg_channel_publisher.py:
def publish_to_channel(post_id: int):
    draft = tg_dispatch.get(post_id)
    telegraph_url = telegraph.create_page(
        title=draft["title"],
        author="Media Fabrique",
        content=build_telegraph_dom(draft["body"])
    )
    send_message(
        chat_id=env("TG_CHANNEL_ID"),
        text=f"{draft['title']}\n\n{draft['summary']}\n\n⚡ <a href=\"{telegraph_url}\">Instant View</a>",
        parse_mode="HTML"
    )
```



Telegram парсит telegraph URL и автоматически рендерит Instant View читателю. **Без Premium**. Бесплатно.

<div align="center">

![Telegraph-страница в браузере: тот же контент, что в WordPress-посте](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.45.14.png)

*Внутри Telegraph-страницы: тот же текст, что в WordPress, но Telegraph не попадает под ad-block'и, не режет featured-картинку и не требует регистрации. Читатель получает «почти полный лонгрид» в Telegram одним нажатием.*

</div>

**Two-stage publish** (Sprint Y) — критическое решение:

```
WP publish (tick=publish)  →  draft_posts.published
                                ↓
                          tg_dispatch (отдельная таблица)
                                ↓
TG publish (tick=publish_tg)  ⤳  tg_dispatch.published_tg
```

WP публикуется независимо от TG. Если TG упал (Telegraph API down, rate limit) — WP-пост остаётся. Если WP упал — TG не публикуется вообще. **Никогда не бывает «TG откатил WP»** — критично для доверия к пайплайну.
WP публикуется независимо от TG. Если TG упал (Telegraph API недоступен, ограничение rate-limiter) — WP-пост остаётся. Если WP упал — TG не публикуется вообще. **Никогда не бывает "TG откатил WP"** — это критично для доверия к конвейеру.

**Rate limits**:
- Telegram Bot API: 30 сообщений/сек на все чаты/каналы, 1 сообщений/сек на каждый чат (одно сообщение в канал в секунду)  
- Фактическое использование в первой версии версии для ПРОД: 30 постов/день × 1 сообщение/секунду = 30 секунд взаимодействия с TG в день. Нормально.  

Подробнее — в [`docs/architecture/06-telegram-specifics.md`](docs/architecture/06-telegram-specifics.md).

---

## 8. Self-host

**Prerequisites** (минимум для одного VPS):

| Что | Требование |
|---|---|
| VPS | 2 GB RAM, 1 vCPU, 20 GB SSD, Ubuntu 24.04 LTS |
| Домен | Свой + DNS A-запись на IP VPS |
| TLS | Let's Encrypt через certbot (бесплатно, авто-renew) |
| Telegram Bot | Токен через [@BotFather](https://t.me/BotFather), бот = admin в канале |
| Telegram Channel | Публичный или приватный (по выбору) |
| LLM API key | OpenAI или OpenAI-совместимый эндпоинт |
| WordPress | 5.8+ с REST API + Application Passwords |

**Стоимость**:

**Периодические (ежемесячно)** — два варианта:

**Вариант 1: pay-as-you-go (рекомендуется для production)** — доступность 100%:
- VPS (2 GB RAM, 1 vCPU): **$8/мес**
- LLM MiniMax-M3 (MiniMax API direct, pay-as-you-go):
  - 6000 пересказов/мес × 3000 input × $0.30/M = **$5.40/мес**
  - 6000 пересказов/мес × 1400 output (800 visible + 600 CoT thinking) × $1.20/M = **$10.08/мес**
  - **LLM итого: $15.48/мес**
- Домен: $2/год (~$0.17/мес)
- Telegraph / WordPress / Telegram: бесплатно
- **Периодические итого: $8 + $15.48 + $0.17 = ~$23.65/мес**

**Вариант 2: подписка MiniMax Starter ($20/мес)** — дешевле, но с риском:
- VPS: $8/мес
- LLM MiniMax-M3 (подписка Starter): **~$8/мес эффективно** ($0.35/M blended, CoT в лимитах)
- Домен: $0.17/мес
- **Периодические итого: ~$16.17/мес** — экономия $7.48/мес (~$90/год)
- **⚠️ Но лимиты могут кончиться → пайплайн встанет** (см. §6 «Подписка vs pay-as-you-go»)

**Единоразовые / по необходимости:**
- Подписка MiniMax для разработки: $20
- Домен (renew): ~$2/год

**5 шагов до первого `tick=publish`**:

```bash
# 1. На сервере: создать пользователя
ssh root@{vps-host}
useradd -m -s /bin/bash {deploy-user}
mkdir -p /opt/{deploy-user}
chown -R {deploy-user}:{deploy-user} /opt/{deploy-user}

# 2. Заклонировать репо
sudo -u {deploy-user} git clone \
  https://github.com/DimbikeY/media-fabrique-template.git \
  /opt/{deploy-user}/media-fabrique-template
sudo -u {deploy-user} python3 -m venv /opt/{deploy-user}/.venv
sudo -u {deploy-user} /opt/{deploy-user}/.venv/bin/pip install \
  -r /opt/{deploy-user}/media-fabrique-template/requirements.txt

# 3. Заполнить .env
sudo -u {deploy-user} cp \
  /opt/{deploy-user}/media-fabrique-template/.env.example \
  /opt/{deploy-user}/.env
sudo -u {deploy-user} chmod 600 /opt/{deploy-user}/.env
$EDITOR /opt/{deploy-user}/.env

# 4. Инициализация БД
sudo -u {deploy-user} cd /opt/{deploy-user}/media-fabrique-template
sudo -u {deploy-user} /opt/{deploy-user}/.venv/bin/python init_db.py
sudo -u {deploy-user} /opt/{deploy-user}/.venv/bin/python migrate.py

# 5. Cron
sudo cp /opt/{deploy-user}/media-fabrique-template/deploy/{project}-pipeline.cron \
        /etc/cron.d/{project}-pipeline
sudo systemctl restart cron
```



**Smoke после развёртывания**:

```bash
sudo -u {deploy-user} /opt/{deploy-user}/.venv/bin/python \
  /opt/{deploy-user}/media-fabrique-template/test_sprint_y_e2e_smoke.py
# Expect: 19/19 PASS
```



Полный гайд — в [`docs/architecture/07-self-host.md`](docs/architecture/07-self-host.md).

---

## 9. Lessons learned (5 главных gotchas)

1. `LANG=C.UTF-8` для cron

Cron по умолчанию запускает jobs с `LANG=C`, что ломает русский текст в Telegram-сообщениях. Решение — в начале каждого cron-strip:

```cron
LANG=C.UTF-8
*/30 * * * *   {deploy-user}  cd /opt/{deploy-user}/media-fabrique-template && .venv/bin/python main.py tick=fetch
```



2. Telegraph IPv6 мёртв, urllib переключён на IPv4

VPS имел IPv6-маршрут к `api.telegra.ph` через сломанный роутер. Timeout 30 секунд. Решение — `urllib3.util.connection.HAS_IPV4` + явный переход на IPv4. Подробнее — Sprint 6d.7.

3. Telegram webhook: last-write-wins — это фича

OpenClaw gateway перезапускается → вызывает `setWebhook` → Telegram помнит только последний. Это нормально — мы хотим один webhook receiver. Не запускайте 2 инстанса telegram_receiver.py одновременно, иначе race condition.

4. WP Application Passwords имеет пробелы

WordPress генерирует пароль в формате `xxxx xxxx xxxx xxxx xxxx xxxx` (24 символа с пробелами). В `.env` нужно экранировать:

```bash
# Неправильно (bash interpretation):
WP_APP_PASSWORD=abcd efgh ijkl mnop qrst uvwx

# Правильно:
WP_APP_PASSWORD="abcd efgh ijkl mnop qrst uvwx"
```



Python `os.environ.get()` сохраняет пробелы, но bash до этого может их потрепать.

5. SQLite single-writer — concurrent write → SQLITE_BUSY

Два cron-тика `tick=publish` одновременно рвутся в SQLite → один получит `SQLITE_BUSY`. Решение — WAL mode + retry в коде:

```python
import sqlite3
conn = sqlite3.connect("data/news_memory.db", timeout=10)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")  # 5 секунд retry
```



WAL mode + 5-second timeout решает 99% race conditions. Оставшийся 1% — это если упадёт сетевой диск, тогда timeout 30 секунд, что норм.

Подробнее — в [`docs/architecture/08-operational-playbook.md`](docs/architecture/08-operational-playbook.md).

---

## 10. CTA — форкни и сделай своё

Если ты читаешь это и думаешь "хм, может попробовать" — форкни [`media-fabrique-template`](https://github.com/DimbikeY/media-fabrique-template) прямо сейчас. Это час работы, чтобы развернуть, и ещё час, чтобы адаптировать под свою тему.

**Что подходит для форка**:
- Отраслевые новости (AI-мир, инвестиции, школы 237)
- Внутрикорпоративные новости — RSS из корпоративных блогов, Slack-каналов
- Мониторинг конкурентов — их блоги, пресс-релизы, Product Hunt
- Нишевые сообщества — HN, Reddit, habr, arxiv по подписке
- Дайджесты исследований — arxiv, PubMed, Google Scholar

**Что НЕ подходит**:
- Высоконагруженные сервисы (>1000 постов/день) — SQLite не успеет обработать
- Прямые эфиры — задержка ~30 мин не годится. Снижайте до 2-5 мин.  
- Многоязыковые с >2 языками — промпт и категории надо переписывать

**Если хочешь такое же для своего бизнеса** — пиши в TG `@DimbikeY`, обсудим scope. Консультационные условия — отдельно.

**Посмотреть как работает в проде**: [@the_vremya](https://t.me/the_vremya) — новостной канал с ежедневной выдачей из этого пайплайна. **NB**: канал может быть удалён в будущем (это личный канал оператора, не часть репозитория). Telegram сохраняет кеш опубликованных сообщений какое-то время после удаления канала — старые посты остаются доступны по этой ссылке даже после возможного удаления.

**Где спросить**:
- GitHub Issues — bug, feature request, question
- Telegram `@DimbikeY` — для всего, что не помещается в issue
- PR приветствуется — после issue, с smoke тестами

**Disclaimer**: Это шаблон, не product. Используй на свой страх и риск, без гарантий. Уважай RSS-источники, Telegram ToS, OpenAI ToS, WordPress ToS. Пересказ ≠ republication — всегда указывай источник.
