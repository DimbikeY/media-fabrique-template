# 05 — Half-life scoring

## TL;DR

Каждый кандидат получает `expires_at` в момент `tick=rewrite`: время,
когда его вес упадёт ниже `MIN_THRESHOLD=0.5`. После этого janitor
его удаляет. Half-life берётся из `CATEGORY_HALF_LIFE_H` по
категории; вес падает экспоненциально: `w = base × 0.5^(age/half_life)`.

## Что такое half-life

Half-life `H` — это **время, за которое вес новости уменьшается вдвое**.
Для `politics` H=6 ч: через 6 часов пост весит 5 (если base_score=10),
через 12 ч — 2.5, через 18 ч — 1.25. Это интуитивно понятнее, чем
линейное затухание: не нужно выбирать «когда считать просроченным»,
достаточно одного параметра, который читается естественно для каждой
тематики (быстрая — спорт, медленная — наука).

## Score function

```python
# scoring.py
def decay(base_score: float, age_h: float, half_life_h: float) -> float:
    """w = base_score * 0.5 ^ (age_h / half_life_h)"""
    if age_h < 0:
        age_h = 0.0
    if half_life_h <= 0:
        return 0.0
    base = abs(float(base_score))
    if base == 0:
        return 0.0
    w = base * (0.5 ** (age_h / half_life_h))
    return 0.0 if w < 1e-12 else w
```

Полная формула:

```
weight = base_score
       × topic_weight      (= 1.0 сейчас; зарезервировано под источники)
       × source_reliability (= 1.0 сейчас; зарезервировано)
       × 0.5 ^ (age_h / half_life_h(category))
```

Сейчас `topic_weight` и `source_reliability` — единица. Расширения
документированы в `models.py` (Sprint 5.1 design notes) — fork'айте
`score_item`, добавьте веса из таблицы `sources` или A/B-теста.

## `expires_at` — зачем важно

`expires_at` решает три проблемы:

1. **TTL для TG-кэша.** Telegram кэширует link preview на ~5 минут;
   если пост отозван через час, preview уже неактуален. Но мы не
   полагаемся на кэш TG — мы полагаемся на `expires_at` в нашей БД,
   который говорит «это уже не стоит публиковать».

2. **Идемпотентный janitor.** SQL
   `DELETE FROM candidates WHERE expires_at < datetime('now') AND status='new'`
   — это один statement, monotonic, без join'ов. Janitor не должен
   знать про категории.

3. **Anti-stuck-row.** Если `tick=publish` застрял (Telegraph API down),
   `failed → draft → publishing → failed` цикл не вечен: после
   `expires_at` janitor **принудительно** удаляет строку, чтобы
   она не уходила в бесконечный retry (см. janitor `purge_expired_processed`).

### Как считается `expires_at`

```python
def compute_expires_at(base_score, fetched_at, half_life_h, min_threshold=0.5):
    base = abs(float(base_score))
    if base <= 0:
        return fetched_at
    if base <= min_threshold:
        return fetched_at + timedelta(hours=1)

    lifetime_h = half_life_h * math.log(min_threshold / base, 0.5)
    lifetime_td = timedelta(hours=float(lifetime_h))
    rounded = timedelta(seconds=math.ceil(lifetime_td.total_seconds() / 60) * 60)
    return fetched_at + rounded
```

Решение уравнения `base × 0.5^(t/H) = threshold` относительно `t`:

```
t = H × log2(threshold / base)
```

`threshold=0.5`, `base=10` (max score), `H=24` (tech): `t = 24 × log2(0.05) ≈ 95 ч ≈ 4 дня`. То есть топ-новость про технологии
живёт в системе ~4 дня до удаления.

## `score_item` — реальный код

```python
def score_item(*, priority, category, fetched_at,
               min_threshold=MIN_THRESHOLD, now=None):
    base = _clamp_priority(priority)
    cat = validate_category(category)
    if category_was_drift(category):
        try:
            from loguru import logger
            logger.warning("score_item: LLM category {!r} not in WHITELIST, coerced to {!r}",
                           category, cat)
        except Exception:
            pass
    hl = CATEGORY_HALF_LIFE_H[cat]
    when = (now or datetime.utcnow()).replace(microsecond=0)
    fetched = fetched_at.replace(microsecond=0) if fetched_at else when
    return ScoringResult(
        base_score=base,
        category=cat,
        half_life_h=hl,
        weight=base,                       # weight == base_score at t=0
        expires_at=compute_expires_at(base, fetched, hl, min_threshold),
        scored_at=when,
    )
```

`ScoringResult.as_db_dict()` отдаёт маппинг для одного UPDATE:

```python
def as_db_dict(self) -> dict[str, object]:
    return {
        "base_score":   self.base_score,
        "category":     self.category,
        "half_life_h":  self.half_life_h,
        "weight":       self.weight,
        "expires_at":   self.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        "scored_at":    self.scored_at.strftime("%Y-%m-%d %H:%M:%S"),
    }
```

`rewrite_and_score._mark_ready_with_score` использует это и пишет
все колонки + `status='ready'` одним SQL:

```python
UPDATE candidates
   SET status='ready', error_reason=NULL,
       base_score=:base_score, category=:category,
       half_life_h=:half_life_h, weight=:weight,
       expires_at=:expires_at, scored_at=:scored_at
 WHERE id=:id
```

## Как влияет на приоритизацию в `tick=rewrite`

`tick=rewrite` не использует scoring для ordering — он берёт все
`status='new'` (старые или только что пришедшие) и обрабатывает в
порядке recency (см. SQL в `rewrite_and_score._fetch_candidates`).
Scoring используется в `tick=publish` — топ-кандидаты уходят в WP
первыми:

```sql
-- publisher._fetch_candidates (Sprint 5.1 ordering)
ORDER BY
  CASE WHEN i.weight IS NULL THEN 1 ELSE 0 END,
  i.weight DESC,
  p.id ASC
```

`NULLS LAST` через `CASE` (SQLite не имеет нативного синтаксиса).
Unscored (NULL weight) идут последними, scored — по убыванию веса.
Чем выше base_score, тем выше в очереди; чем дольше лежит, тем
weight меньше → топ-новости публикуются первыми.

`tick=publish_tg` использует тот же JOIN на candidates — TG-канал
получает то же top-weight подмножество, что и WP.

## Extension point: кастомный scoring function

```python
# my_scoring.py — форк scoring.py
from scoring import decay, CATEGORY_HALF_LIFE_H, MIN_THRESHOLD, DEFAULT_CATEGORY

SOURCE_WEIGHT = {
    "IGN (EN)": 1.2,
    "Eurogamer (EN)": 1.1,
    # ...
}

def score_with_source(*, priority, category, source_name, fetched_at, now=None):
    base = priority
    cat = validate_category(category)
    sw = SOURCE_WEIGHT.get(source_name, 1.0)
    base *= sw
    # дальше всё как в score_item...
```

Чтобы подключить:

```python
# rewrite_and_score.py
# from scoring import score_item
from my_scoring import score_with_source as score_item
```

Никаких миграций, никаких изменений в `init_db.py` — `weight` это
просто число с плавающей точкой.

## Где форкать

| Хочется | Файл |
|---|---|
| Добавить вес источника | форк `scoring.score_item` (см. выше) |
| Поменять порог удаления | `scoring.MIN_THRESHOLD` (через `MIN_THRESHOLD=` kwarg в `score_item`) |
| Поменять half-life категории | `scoring.CATEGORY_HALF_LIFE_H` |
| Поменять формулу decay (линейная, square) | `scoring.decay` |
| Изменить ordering в publish | `publisher._fetch_candidates` (SQL `ORDER BY`) |
| Изменить каденс janitor | `config.py:PIPE_TICKS.janitor_cron` |

## Следующее

- [`02-state-machine.md`](02-state-machine.md) — `failed → draft` цикл и роль `expires_at`.
- [`04-categorization.md`](04-categorization.md) — связь категория → half-life.
- [`07-self-host.md`](07-self-host.md) — где найти `expires_at` в дампе SQLite.
