# Media Fabrique Template: how to build a self-hosted news pipeline and why it's cheaper than it looks

*Bilingual longread. English version — `longread-en.md`. Companion GitHub repository: `media-fabrique-template`.*

---

## 1. Hook

Six in the morning. The RSS reader is open in the next tab, and the feeds have accumulated 200+ unread headlines overnight. Among them there are typically 2-3 important news items, plus another 5-6 that are potentially interesting for the channel. The remaining 190 are junk that will not be read and is not needed.

This gap — between the RSS stream and what is usually worth paying attention to — is the reason **Media Fabrique** was built. It is a self-hosted pipeline that pulls RSS feeds, hands the texts to an LLM for rewrites, scores them with a half-life function, generates an image, and publishes to WordPress and a Telegram channel. State lives in SQLite, orchestration is handled by system cron. The **development budget** is ~$40 one-time (initial VPS setup, annual domain rental, MiniMax development subscription for the first few months). The **maintenance budget** is **$24/month via pay-as-you-go** (recommended for production, 100% availability) or **$16/month via the MiniMax Starter subscription** (saves ~$90/year, but limits can run out and the pipeline can stop).

It was started as an experiment for a news channel. The reusable core was extracted — this is already a template, not a production service. Fork it, swap the RSS sources for your own, rewrite the prompt to fit your topics, point it at your WordPress and Telegram — within an hour there is a working pipeline. The entire codebase is open (MIT license), is self-checked with smoke tests, and is deployed with a single script.

This longread is a full breakdown: why, how it is built, where the pitfalls are, and what should be redesigned. If self-hosted automation at the intersection of LLMs, RSS, and pipeline orchestration sounds interesting — let us proceed.

<div align="center">

![A finished Telegram channel post with Telegraph Instant View](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.44.52.png)

*A finished Telegram channel post: Telegraph Instant View (⚡) — the full article opens with one tap, without Telegram Premium. The same text and image as the WordPress version, but faster and without restrictions.*

</div>

---

## 2. What is Media Fabrique

**Media Fabrique** is not a product, it is a template. The kind of thing that gets forked, adapted to one's needs, and then forgotten until the next time. Under the hood there is a working pipeline that has been automatically publishing 30+ posts a day to a news channel on a VPS for about a month, costing only a few dollars per month.

**What it does**:
- Polls RSS feeds every 30 minutes
- Sends the candidate content to an LLM with the instruction "evaluate, rewrite, do not miss spam"
- Scores via a half-life function per category (younger = higher priority); AI news decays faster than culture news
- Generates a featured image (300×200 px WebP)
- Publishes to WordPress via the REST API
- In parallel, prepares a Telegram preview with a Telegraph mirror for Instant View

**What it does not do**:
- Does not write original content (only rewrites)
- Does not run social media accounts (only one TG channel)
- Has no UI (only CLI + a TG bot for moderation)

**Numbers**:
- 12 RSS feeds, ~700-1200 candidates per day
- The LLM filter rejects ~85%, ~150 rewrites per day
- Scoring picks the top-30 by half-life
- 30 channel publications + 30 WordPress posts
- News channel: [@the_vremya](https://t.me/the_vremya)
- VPS: 2 GB RAM, 1 vCPU, $8/month
- LLM (pay-as-you-go, recommended for production): **$15.48/month** ($0.30/M input + $1.20/M output, ~22.8M tokens including CoT)
- LLM (MiniMax Starter subscription $20/month): ~$8/month effective, **but limits can run out → pipeline stops** (see §6)
- Domain: ~$2 per year
- MiniMax subscription for development: $20 (separate, for OpenClaw, not for the LLM-API pipeline)
- **Total recurring expenses (pay-as-you-go): ~$23.65/month** — 100% availability, no limit risk
- Total recurring expenses (subscription): ~$16/month — ~$90/year savings, **but downtime risk**
- Total one-time expenses: ~$22
- Total expenses for the first month (pay-as-you-go): ~$46
- Total expenses for subsequent months (pay-as-you-go): ~$24/month

<div align="center">

![Morning report, July 26: 38 publications in 24h, category breakdown, $0.58 LLM cost](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.47.17.png)

*Morning report in the moderation TG group: how many were published, how many were skipped, how much was spent on the LLM. Every day, automatically, visible — no hidden metrics in Grafana.*

</div>

**Stack**:
- Python 3.11+ `.venv` (no venv managers)
- SQLite for state — migrations via `migrate.py`
- OpenAI Python SDK (openai>=1.40) — works with any OpenAI-compatible endpoint
- WordPress REST API + Application Passwords
- Telegram Bot API + Telegraph API
- Pillow (WebP encode)
- feedparser + readability-lxml (HTML normalization)
- System cron for orchestration

---

## 3. Architecture

One pipeline = several cron ticks. Each tick reads SQLite, performs one focused piece of work, and updates the DB state. No central loop, no event bus, no message queue.

```
┌─────────────┐
│  RSS feeds  │  12 sources, ~700-1200 candidates/day
└──────┬──────┘
       │ tick=fetch (every 30 min)
       ▼
┌─────────────────────────────┐
│ candidates (SQLite)         │  ~1000 new per day
│ status: new                 │
└──────┬──────────────────────┘
       │ tick=rewrite (every 7 min)
       ▼
┌─────────────────────────────┐
│ LLM rewrite + scoring       │  minimax-m3 (+ rollback to 2.7-highspeed), ~22.8M tokens/month
│ - stop-topics (N1, N2)      │
│ - category from 12 (whitelist) │
│ - half-life score           │
└──────┬──────────────────────┘
       │ (for top-30 by score)
       ▼
┌─────────────────────────────┐
│ draft_posts (SQLite)        │  ~30 ready per day
│ status: ready               │
└──────┬──────────────────────┘
       │ tick=publish (every 5 min)
       ▼
┌─────────────────────────────┐
│ WordPress REST API          │  status=publish (auto)
│ + featured image (WebP)     │
└─────────────────────────────┘

       │ (parallel branch)
       │
┌─────────────────────────────┐
│ tick=generate_for_tg        │  separate LLM call with short prompt
│ → tg_dispatch.awaiting      │
└──────┬──────────────────────┘
       │ tick=publish_tg (every 10 min)
       ▼
┌─────────────────────────────┐
│ Telegram channel publish    │  Telegraph mirror for IV
└─────────────────────────────┘
```

**Why not Celery / Airflow / Temporal**:

- One VPS, 30 posts/day, 2 GB RAM — the overhead of a task manager costs more than the tasks themselves
- Cron already handles scheduling, retries (if configured), and alerting (via logwatch)
- Only 6 ticks in total — fewer than the number of types in Airflow
- Each tick is idempotent — if it crashes, the next tick picks up from where it left off

A detailed breakdown of the cron architecture is in [`docs/architecture/03-cron-architecture.md`](docs/architecture/03-cron-architecture.md).

<div align="center">

![TG bot assistant: `/preview`, `/approve`, `/reject`, `/feedback` commands for draft_post 655](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.46.38.png)

*Every draft goes through a human before publication: the bot sends a preview to the moderation TG group, the operator presses `/preview` → reads → `/approve` (publish) or `/reject` (decline). Auto-publish is enabled with one env variable, but defaults to human-in-the-loop.*

</div>

---

## 4. State machine in SQLite

All three key entities — `candidates`, `draft_posts`, `tg_dispatch` — are **finite state machines on SQL**. No Redis, no locks, no actor model. Each state transition is an `UPDATE ... WHERE id=? AND status=?` with a `cur.rowcount == 1` check.

```sql
-- Atomic claim: "WE" are the only ones moving ready → publishing
UPDATE draft_posts
SET status = 'publishing', claimed_at = now()
WHERE id = ? AND status = 'ready';
-- If rowcount == 1 — it is ours. If 0 — someone else has already taken it.
```

**Three tables, three state machines**:

**`candidates`** (new → rewrite → done / skipped / failed):
- `new` — just came from RSS
- `rewriting` — the LLM is currently working on it
- `ready` — the rewrite exists, the score exists, and a draft_posts row has been created
- `skipped` — the LLM said "blocked=true" (stop-topic)
- `failed` — the LLM crashed / produced invalid JSON

**`draft_posts`** (draft → approved → published, or rejected):
- `ready` — the LLM rewrite exists, but the image is not yet there
- `image_ready` — the image is ready
- `publishing` — `tick=publish` is currently publishing to WP
- `published` — WP POST/PATCH ok, the post is live
- `failed` — WP error
- `rejected` — rejected (via `/reject` in the TG bot)

**`tg_dispatch`** (TG-preview → approved → published, or rejected):
- `pending_tg_text` — awaiting `tick=generate_for_tg`
- `awaiting_approval` — the preview has been sent to the moderation TG group
- `approved` — `/approve_tg` was pressed
- `published_tg` — TG channel publish ok
- `rejected_tg` — `/reject_tg` was pressed

**Race conditions** are avoided in two ways:

1. **Atomic UPDATE with rowcount==1** — each tick picks up only one row under the update
2. **`error_reason = 'worker:HOST:PID'`** — a soft marker that records who is currently working. If a handler dies, the last handler id remains in place. This is useful for forensic analysis.

**Why SQLite, not Redis**:

- A single writer in SQLite **is** the lock. No distributed mutex is required.
- WAL mode: writes do not block reads.
- A single source of truth in one file — `rsync` it as a regular file for backups.
- Migrations via `migrate.py` are idempotent and tracked in the `_migrations` table.

When SQLite will not be enough: >100 records/sec.

A detailed breakdown is in [`docs/architecture/02-state-machine.md`](docs/architecture/02-state-machine.md).

---

## 5. Cron instead of an orchestrator

**Schedule** (`/etc/cron.d/<project>-pipeline`):

```cron
# Every 30 minutes — fetch RSS
*/30 * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=fetch

# Every 7 minutes — rewrite via LLM
*/7  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=rewrite

# Every 5 minutes — publish to WordPress
*/5  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=publish

# Every 5 minutes — generate TG preview
*/5  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=generate_for_tg

# Every 10 minutes — publish TG
*/10 * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=publish_tg

# Every hour — janitor (delete expired, heal stuck)
0    * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=janitor
```

**What cron gives over Celery**:

- Cron already handles: scheduling, logs, ENVIRONMENT, and user context
- Cron is easier to monitor: `grep cron /var/log/syslog` — all ticks are available to read
- Cron does not fail as a whole — if one tick fails, the rest keep working
- Cron does not require a separate worker — on a VPS the same `python main.py` runs

**What cron cannot do** (and why that is OK):

- No retry queue — but each tick is idempotent, so a retry is just the next tick
- No priorities — all ticks are equally important
- No UI — but cron does not need a UI, it is just cron

**Long-running webhook** — a separate systemd unit:

```ini
# /etc/systemd/system/<project>-telegram-receiver.service
[Unit]
Description=Media Fabrique Telegram webhook receiver
After=network.target

[Service]
Type=simple
User=<deploy-user>
WorkingDirectory=/opt/<deploy-user>/media-fabrique-template
ExecStart=/opt/<deploy-user>/.venv/bin/python telegram_receiver.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Backoff, jitter, and error handling** — in the code of each tick (`main.py`):

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

Each tick is isolated and does not bring down the parent process. An error in `tick=publish` does not break `tick=fetch`.

More details are in [`docs/architecture/03-cron-architecture.md`](docs/architecture/03-cron-architecture.md).

---

## 6. LLM integration

**Provider**: minimax m3. Cheap, sharp, high limits, low price, and no sanctions restrictions.

**Cost** (current MiniMax API rates, 2026):

| Parameter | MiniMax Subscription (Starter, $20/month) | Pay-as-you-go (MiniMax API direct) |
|---|---|---|
| Input | included in subscription | **$0.30 / 1M tokens** |
| Output | included in subscription | **$1.20 / 1M tokens** |
| Cache read | — | $0.06 / 1M tokens |
| CoT (60-120s) | included in subscription limits | billed as output |
| Blended rate | ~$0.35/M tokens | ~$0.65-0.75/M tokens |
| Total on 22.8M tokens/month | **~$8/month effective** | **~$15-17/month effective** |

Pay-as-you-go calculation (with CoT thinking tokens):
- 200 rewrites/day × 30 days = 6000 rewrites/month
- Per rewrite: 3000 input + 1400 output (800 visible + 600 thinking) = 4400 tokens
- Input: 6000 × 3000 × $0.30/M = **$5.40/month**
- Output: 6000 × 1400 × $1.20/M = **$10.08/month**
- **Total: ~$15.48/month** (~$0.65/M blended). With real category mix: ~$15-17/month.

Subscription calculation:
- MiniMax Starter ($20/month) includes CoT and rate limits; the effective cost at our volume is ~$8/month ($0.35/M blended). No hard caps above the subscription tier; limits reset every 5 hours, weekly and monthly.

**⚠️ Subscription vs pay-as-you-go for production**:

Subscription is ~$7.5/month cheaper (~$90/year), but it has a **critical downside for production** — limits can run out, and the pipeline stops. This means:
- Sudden pipeline downtimes at peak hours, without warning
- Need 24/7 monitoring of limit consumption (a separate concern)
- Painful scaling: adding 5 RSS sources = +30-50% to token usage → limit runs out faster → queue stops
- CoT priority is lower than pay-as-you-go customers — during spikes, their requests are served first

Pay-as-you-go for a production pipeline:
- **100% availability** — no limits, the pipeline never stops
- CoT tokens are prioritised at the same level as regular ones
- Flexible scaling — add an RSS source without thinking about limits
- Extra ~$7.5/month (~$90/year) — the cost of preventing downtime

**Recommendation**: for a production pipeline with dozens of posts per day, pay-as-you-go is the right choice. Subscriptions are for experiments and prototypes with controlled volume.

**Token usage**: 22.8M/month (calculated: 200 × 3800 × 30). Real blended with CoT is higher.

**Stop-topics** — a JSON flag in the LLM response:

```python
# In the system prompt:
"If the news is about N1/N2/N3/N4 — return {\"blocked\": true, \"reason\": \"N1\"}"

# In code:
result = llm_client.complete(...)
if result.get("blocked"):
    candidates._mark_skipped(candidate_id, reason=result["reason"])
    return
```

**Categorization** — a closed set of 12 topics (Sprint 6.7):

```python
CATEGORIES_WHITELIST = [
    "tech", "business", "science", "health", "sport",
    "culture", "cinema", "gaming", "auto", "travel",
    "food", "misc"
]
```

The LLM does not classify at runtime — that is deterministic. Instead, the prompt asks the model to **pick one category from the list**. This is:
- cheaper (no extra tokens spent on classification)
- more stable (no "drift" between runs)
- easier to validate (bad categories are easy to grep for)

**Prompt version pinning** (`prompts.TG_PROMPT_VERSION`):

```python
# Masthead prompt version, e.g. "v2.3"
# When the prompt changes — bump the version
# The master prompt and the TG prompt have separate versions
```

If a new prompt performs worse — roll back via `prompts.TG_PROMPT_VERSION = "v2.2"` and fix the regression without retraining.

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

3 attempts, exponential backoff 1-10 seconds. If all 3 fail — the candidate is moved to `failed`, and the janitor will retry in an hour.

More details are in [`docs/architecture/04-categorization.md`](docs/architecture/04-categorization.md).

---

## 7. Telegram specifics

**Long-poll vs Webhook** — and why webhook fits better under the current constraints.

Long-poll (the default used by `python-telegram-bot`):
- Easier to configure (no public URL needed)
- Works behind NAT
- But: keeps an HTTP connection open constantly, occupies a worker slot, and is slower

Webhook:
- Requires a public URL + TLS
- Telegram itself sends an HTTP POST to the endpoint
- Faster, and does not occupy a worker
- Registered via the `setWebhook` API

Telegraph + Instant View — a replacement for Telegram Premium in the context of rich editing options.

```python
# In tg_channel_publisher.py:
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

Telegram parses the Telegraph URL and automatically renders Instant View for the reader. **Without Premium**. Free.

<div align="center">

![Telegraph page in the browser: the same content as the WordPress post](images/screenshots/cropped-Screenshot%202026-07-26%20at%2014.45.14.png)

*Inside the Telegraph page: the same text as WordPress, but Telegraph does not fall under ad blockers, does not crop the featured image, and does not ask for registration. The reader gets a "near-full longread" in Telegram with one tap.*

</div>

**Two-stage publish** (Sprint Y) — a critical decision:

```
WP publish (tick=publish)  →  draft_posts.published
                                ↓
                          tg_dispatch (separate table)
                                ↓
TG publish (tick=publish_tg)  ⤳  tg_dispatch.published_tg
```

WP publishes independently of TG. If TG fails (Telegraph API unavailable, rate-limiter hit) — the WP post remains. If WP fails — TG does not publish at all. **There is never a "TG rolled back WP"** — that is critical for trust in the pipeline.

**Rate limits**:
- Telegram Bot API: 30 messages/sec across all chats and channels, 1 message/sec per chat (one message per channel per second)
- Actual usage in the first PROD version: 30 posts/day × 1 message/sec = 30 seconds of interaction with TG per day. That is fine.

More details are in [`docs/architecture/06-telegram-specifics.md`](docs/architecture/06-telegram-specifics.md).

---

## 8. Self-host

**Prerequisites** (minimum for one VPS):

| What | Requirement |
|---|---|
| VPS | 2 GB RAM, 1 vCPU, 20 GB SSD, Ubuntu 2N.0T LTS |
| Domain | Own one + DNS A-record pointing to the VPS IP |
| TLS | Let's Encrypt via certbot (free, auto-renew) |
| Telegram Bot | Token via [@BotFather](https://t.me/BotFather), bot = admin in the channel |
| Telegram Channel | Public or private (your choice) |
| LLM API key | OpenAI or an OpenAI-compatible endpoint |
| WordPress | 5.8+ with REST API + Application Passwords |

**Cost**:

**Recurring (monthly)** — two options:

**Option 1: pay-as-you-go (recommended for production)** — 100% availability:
- VPS (2 GB RAM, 1 vCPU): **$8/month**
- LLM MiniMax-M3 (MiniMax API direct, pay-as-you-go):
  - 6000 rewrites/month × 3000 input × $0.30/M = **$5.40/month**
  - 6000 rewrites/month × 1400 output (800 visible + 600 CoT thinking) × $1.20/M = **$10.08/month**
  - **LLM total: $15.48/month**
- Domain: $2/year (~$0.17/month)
- Telegraph / WordPress / Telegram: free
- **Recurring total: $8 + $15.48 + $0.17 = ~$23.65/month**

**Option 2: MiniMax Starter subscription ($20/month)** — cheaper, but with risk:
- VPS: $8/month
- LLM MiniMax-M3 (Starter subscription): **~$8/month effective** ($0.35/M blended, CoT within limits)
- Domain: $0.17/month
- **Recurring total: ~$16.17/month** — saves $7.48/month (~$90/year)
- **⚠️ But limits can run out → pipeline stops** (see §6 «Subscription vs pay-as-you-go»)

**One-time / as needed:**
- MiniMax subscription for development: $20
- Domain (renew): ~$2/year

**5 steps to the first `tick=publish`**:

```bash
# 1. On the server: create the user
ssh root@<vps-host>
useradd -m -s /bin/bash <deploy-user>
mkdir -p /opt/<deploy-user>
chown -R <deploy-user>:<deploy-user> /opt/<deploy-user>

# 2. Clone the repo
sudo -u <deploy-user> git clone \
  https://github.com/DimbikeY/media-fabrique-template.git \
  /opt/<deploy-user>/media-fabrique-template
sudo -u <deploy-user> python3 -m venv /opt/<deploy-user>/.venv
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/pip install \
  -r /opt/<deploy-user>/media-fabrique-template/requirements.txt

# 3. Fill in .env
sudo -u <deploy-user> cp \
  /opt/<deploy-user>/media-fabrique-template/.env.example \
  /opt/<deploy-user>/.env
sudo -u <deploy-user> chmod 600 /opt/<deploy-user>/.env
$EDITOR /opt/<deploy-user>/.env

# 4. Initialize the DB
sudo -u <deploy-user> cd /opt/<deploy-user>/media-fabrique-template
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/python init_db.py
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/python migrate.py

# 5. Cron
sudo cp /opt/<deploy-user>/media-fabrique-template/deploy/<project>-pipeline.cron \
        /etc/cron.d/<project>-pipeline
sudo systemctl restart cron
```

**Smoke after deployment**:

```bash
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/python \
  /opt/<deploy-user>/media-fabrique-template/test_sprint_y_e2e_smoke.py
# Expect: 19/19 PASS
```

The full guide is in [`docs/architecture/07-self-host.md`](docs/architecture/07-self-host.md).

---

## 9. Lessons learned (5 main gotchas)

**1. `LANG=C.UTF-8` for cron**

Cron by default runs jobs with `LANG=C`, which breaks Russian text in Telegram messages. The fix is to put `LANG=C.UTF-8` at the top of each cron strip:

```cron
LANG=C.UTF-8
*/30 * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=fetch
```

**2. Telegraph IPv6 is dead, urllib was switched to IPv4**

The VPS had an IPv6 route to `api.telegra.ph` going through a broken router. 30-second timeout. The fix: `urllib3.util.connection.HAS_IPV4` + an explicit switch to IPv4. More details — Sprint 6d.7.

**3. Telegram webhook: last-write-wins is a feature**

When the OpenClaw gateway restarts, it calls `setWebhook` and Telegram only remembers the last one. This is **normal** — only one webhook receiver is wanted. Do not run 2 instances of `telegram_receiver.py` at the same time, otherwise there is a race condition.

**4. WP Application Passwords has spaces**

WordPress generates the password in the format `xxxx xxxx xxxx xxxx xxxx xxxx` (24 characters with spaces). In `.env` it needs to be escaped:

```bash
# Wrong (bash interpretation):
WP_APP_PASSWORD=*** efgh ijkl mnop qrst uvwx

# Right:
WP_APP_PASSWORD="abcd efgh ijkl mnop qrst uvwx"
```

Python's `os.environ.get()` preserves the spaces, but bash can mangle them before that.

**5. SQLite single-writer — concurrent write → SQLITE_BUSY**

Two cron ticks of `tick=publish` hitting SQLite at the same time → one gets `SQLITE_BUSY`. The fix: WAL mode + retry in code:

```python
import sqlite3
conn = sqlite3.connect("data/news_memory.db", timeout=10)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")  # 5-second retry
```

WAL mode + a 5-second timeout solves 99% of race conditions. The remaining 1% is when the network disk goes down — in that case a 30-second timeout is fine.

More details are in [`docs/architecture/08-operational-playbook.md`](docs/architecture/08-operational-playbook.md).

---

## 10. CTA — fork it and make it yours

If reading this brings the thought "hmm, maybe try it" — fork [`media-fabrique-template`](https://github.com/DimbikeY/media-fabrique-template) right now. It takes about an hour to deploy, and another hour to adapt to a chosen topic.

**What fits for a fork**:
- Industry news (AI world, investments, schools 237)
- Internal corporate news — RSS from corporate blogs and Slack channels
- Competitor monitoring — their blogs, press releases, Product Hunt
- Niche communities — HN, Reddit, habr, arxiv by subscription
- Research digests — arxiv, PubMed, Google Scholar

**What does NOT fit**:
- High-load services (>1000 posts/day) — SQLite will not keep up
- Live streams — ~30-minute latency is not acceptable. Drop it to 2-5 minutes.
- Multilingual with >2 languages — the prompt and categories need to be rewritten

**If the same kind of setup is wanted for a business** — write to TG `@DimbikeY`, and the scope can be discussed. Consulting terms are separate.

**See it in production**: [@the_vremya](https://t.me/the_vremya) — a news channel with daily output from this pipeline. **NB**: the channel may be deleted in the future (it is the operator's personal channel, not part of the repository). Telegram keeps a cache of published messages for some time after a channel is deleted — older posts remain accessible via this link even after potential deletion.

**Where to ask questions**:
- GitHub Issues — for bug reports, feature requests, and questions
- Telegram `@DimbikeY` — for anything that does not fit into an issue
- PRs are welcome — after opening an issue, and with smoke tests included

**Disclaimer**: This is a template, not a product. Use at your own risk, without guarantees. Respect RSS sources, Telegram ToS, OpenAI ToS, and WordPress ToS. Rewrite ≠ republication — always cite the source.

---

*This longread is bilingual. English version with all code snippets and diagrams — `longread-en.md`. Companion GitHub repo: `media-fabrique-template`. Telegram channel: `@DimbikeY`.*
