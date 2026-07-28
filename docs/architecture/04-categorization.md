# 04 — Категоризация: закрытое множество (closed-set)

## TL;DR

LLM **не классифицирует** категории в runtime. Модель только выбирает
значение из whitelist'а из 12 ключей, а single source of truth живёт
в `scoring.py:CATEGORY_HALF_LIFE_H`. Whitelist одновременно
используется как WP-slug, half-life-метка, и Pydantic Literal-валидация.

## Закрытое множество

```python
# scoring.py
CATEGORY_HALF_LIFE_H: dict[str, float] = {
    "politics":      6.0,
    "sports":        12.0,
    "ai":            12.0,   # AI-news halves every 12h
    "entertainment": 18.0,
    "business":      24.0,
    "tech":          24.0,
    "vibe-coding":   24.0,   # coding-tools evolve monthly
    "health":        36.0,
    "other":         36.0,
    "culture":       48.0,
    "science":       72.0,
    "nature":        168.0,  # one week
}
WHITELIST: frozenset[str] = frozenset(CATEGORY_HALF_LIFE_H.keys())
DEFAULT_CATEGORY = "other"
```

Эти 12 ключей живут в трёх местах одновременно:

| Место | Роль |
|---|---|
| `scoring.CATEGORY_HALF_LIFE_H` | source of truth + half-life |
| `models.RewriteOutput.category` (Pydantic Literal) | валидация LLM-выхода |
| `init_db.py:SCHEMA` (CHECK constraint) | backstop на `candidates.category` |
| `draft_posts.categories_json` | массив до 2 slug'ов для WP |

Defense-in-depth: даже если Python-код забудет нормализовать,
SQLite CHECK не пропустит мусор в БД. Например:

```sql
category TEXT CHECK (category IS NULL OR category IN (
    'politics','sports','ai','entertainment','business','tech',
    'vibe-coding','health','other','culture','science','nature'
)),
```

## Почему не LLM-классификация в runtime

Чтобы присвоить категорию через LLM, есть три подхода:

1. **Свободный текст** ("политика", "технологии", "AI/ML"). Минусы:
   модель галлюцинирует новые ярлыки, downstream-таблицы
   разбухают, half-life lookup возвращает default для всего.
2. **Отдельная LLM-сессия классификации**. Минусы: +latency (+2-5с
   на rewrite), +tokens (ещё 1-2k prompt+completion), +стоимость.
   На 30 постах/день это ~$0.50/день впустую.
3. **Closed-set (наш выбор)**. LLM видит в `master_prompt.md`
   список 12 ключей и в JSON отвечает одним из них. Минусы:
   нужно поддерживать whitelist в sync с WP-таксономией.

Trade-off явно в пользу determinism: half-life поведение полностью
предсказуемо, drift в категориях видно сразу через warning в логе
(`score_item: LLM category "sport" not in WHITELIST, coerced to "sports"`).
Время классификации = 0 мс (одно Pydantic-валидация в одном JSON).

### Prompt version pinning

`prompts.TG_PROMPT_VERSION` хранит версию промпта, которая пишется
в `tg_dispatch.prompt_version`. Это позволяет:

- Разделять посты «по старому промпту» и «по новому» в одном дампе.
- Re-batch старых постов под новую версию промпта, если формулировка
  TG-preview изменилась.

```python
TG_PROMPT_VERSION = "master_prompt_tg.md@v1.0"
```

Тот же паттерн используется для основного промпта в
`llm_runs.stage` (отдельный столбец, не версия) — `stage=ingest |
llm_parse | llm_validate | llm_request | llm_auth | llm_quota | db_write`.

## Как добавить новую категорию

Допустим, вы хотите добавить категорию `crypto` с half-life 6 ч.

### Шаг 1 — обновить whitelist

`scoring.py`:

```python
CATEGORY_HALF_LIFE_H: dict[str, float] = {
    ...
    "crypto":        6.0,    # ← новая категория
    ...
}
WHITELIST = frozenset(CATEGORY_HALF_LIFE_H.keys())
```

### Шаг 2 — обновить SQLite CHECK constraint

SQLite **не поддерживает** `ALTER TABLE ... DROP CONSTRAINT`.
Самый дешёвый путь — добавить CHECK заново через миграцию:

```python
# migrate.py — добавить новую миграцию
def migration_022_add_crypto_category(conn):
    # SQLite: пересоздаём таблицу с новым constraint
    conn.executescript("""
        CREATE TABLE candidates_new (... WITH CHECK (... 'crypto' ...));
        INSERT INTO candidates_new SELECT * FROM candidates;
        DROP TABLE candidates;
        ALTER TABLE candidates_new RENAME TO candidates;
    """)
```

Подробности SQLite-пересоздания таблиц — в
[`08-operational-playbook.md`](08-operational-playbook.md) раздел
"миграции".

### Шаг 3 — обновить промпт и Pydantic

`master_prompt.md` — добавить категорию в список правила #11.
`models.py:RewriteOutput` — расширить `Literal[...]` (если
используется LLM-only Pydantic, иначе валидация идёт через
`validate_category`).

### Шаг 4 — создать WP-термин

```bash
ssh <vps-host> 'sudo -u www-data wp term create category crypto \
  --slug=crypto --description="Криптовалюты и блокчейн"'
```

### Шаг 5 — деплой

```bash
# На VPS
cd /opt/<deploy-user>/media-fabrique-template
git pull
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/python migrate.py
sudo systemctl restart <project>-telegram-receiver.service
```

После `tick=rewrite` новые посты начнут попадать в категорию
`crypto` (если LLM её выберет). Старые посты остаются со старыми
категориями — обратной миграции нет, half-life уже посчитан.

## Extension point: пересканировать или пересмотреть

Самый дешёвый способ поменять логику категоризации — отредактировать
`CATEGORY_HALF_LIFE_H` и запустить `migrate.py`. Дороже —

- Изменить `validate_category` чтобы не coerce'ить в `other`, а
  рейзить `ValueError`. Полезно для отладки, но ломает контракт
  «graceful degradation» (см. docstring в `validate_category`).
- Добавить вторую LLM-сессию классификации (двухступенчатый пайплайн)
  — нужно пересмотреть весь `rewrite_and_score.py` + `master_prompt.md`.

## Где форкать

| Хочется | Файл |
|---|---|
| Добавить/убрать категорию | `scoring.py:CATEGORY_HALF_LIFE_H` + миграция в `migrate.py` |
| Поменять half-life значение | `scoring.py` (только число) |
| Жёстко рейзить на drift | `scoring.py:validate_category` (заменить coerce на `raise`) |
| Добавить две категории на пост вместо одной | `models.py:_normalize_categories` (снять `[:2]`) |
| Свой mapping category → TG-hashtag префикс | `master_prompt_tg.md` правило #N |

## Следующее

- [`05-half-life-scoring.md`](05-half-life-scoring.md) — как категория определяет `expires_at`.
- [`06-telegram-specifics.md`](06-telegram-specifics.md) — как категория попадает в TG-preview.
- [`02-state-machine.md`](02-state-machine.md) — CHECK-constraint в `init_db.py:SCHEMA`.
