#!/bin/bash
# mf-daily-stats.sh — собрать суточную статистику и положить в JSON
# VPS-B ПАССИВНЫЙ: только собирает данные, ничего не отправляет
# 
# Запускается cron в 06:30 MSK (= 03:30 UTC).

set -euo pipefail

DB="/opt/<deploy-user>/media-fabrique-template/data/news_memory.db"
OUT_DIR="/opt/<deploy-user>/data/daily-stats"
DATE="$(date +%Y-%m-%d)"
TS="$(date -Iseconds)"
OUT="$OUT_DIR/$DATE.json"

mkdir -p "$OUT_DIR"

# Вчерашние сутки (06:30 предыдущего дня → 06:30 текущего)
# (используем 06:30 — крон VPS-A запускается в 06:45 MSK)
FROM="$(date -d 'yesterday 06:30:00' '+%Y-%m-%d %H:%M:%S')"
TO="$(date -d 'today 06:30:00' '+%Y-%m-%d %H:%M:%S')"

# B15 фикс: проверяем что БД существует и не пустая
if [ ! -f "$DB" ]; then
    echo "[mf-daily-stats] FAIL: DB not found at $DB"
    exit 1
fi

python3 <<PYEOF
import sqlite3, json, sys, os

db = "$DB"
out = "$OUT"
date = "$DATE"
from_ts = "$FROM"
to_ts = "$TO"
ts = "$TS"

def safe_count(cur):
    try:
        return cur.fetchone()[0]
    except sqlite3.OperationalError as e:
        # B15 фикс: если таблица/колонка не существует, вернуть 0
        print(f"[mf-daily-stats] WARN: {e}", file=sys.stderr)
        return 0

if not os.path.exists(db):
    print(f"[mf-daily-stats] FAIL: DB not found: {db}", file=sys.stderr)
    sys.exit(1)

conn = sqlite3.connect(db)

# B3 фикс: candidates.fetched_at (новые) и status='ready' (готовые)
fetched = safe_count(conn.execute(
    "SELECT COUNT(*) FROM candidates WHERE fetched_at >= ? AND fetched_at < ?",
    (from_ts, to_ts)
))
ready = safe_count(conn.execute(
    "SELECT COUNT(*) FROM candidates WHERE status='ready' AND fetched_at >= ? AND fetched_at < ?",
    (from_ts, to_ts)
))
skipped = safe_count(conn.execute(
    "SELECT COUNT(*) FROM candidates WHERE safety_status='skipped' AND fetched_at >= ? AND fetched_at < ?",
    (from_ts, to_ts)
))
failed_cand = safe_count(conn.execute(
    "SELECT COUNT(*) FROM candidates WHERE status='failed' AND fetched_at >= ? AND fetched_at < ?",
    (from_ts, to_ts)
))

# B3+B14 фикс: используем telegram_sent=1 как маркер "пост реально опубликован в TG"
# (janitor обновляет updated_at, но telegram_sent меняется только при успешной публикации)
published = safe_count(conn.execute(
    "SELECT COUNT(*) FROM draft_posts WHERE status='published' AND telegram_sent=1 AND updated_at >= ? AND updated_at < ?",
    (from_ts, to_ts)
))
failed_posts = safe_count(conn.execute(
    "SELECT COUNT(*) FROM draft_posts WHERE status='failed' AND telegram_sent=0 AND updated_at >= ? AND updated_at < ?",
    (from_ts, to_ts)
))

# llm_runs
llm_calls = safe_count(conn.execute(
    "SELECT COUNT(*) FROM llm_runs WHERE started_at >= ? AND started_at < ?",
    (from_ts, to_ts)
))
llm_errors = safe_count(conn.execute(
    "SELECT COUNT(*) FROM llm_runs WHERE started_at >= ? AND started_at < ? AND status != 'ok'",
    (from_ts, to_ts)
))

stats = {
    "date": date,
    "window": {"from": from_ts, "to": to_ts},
    "generated_at": ts,
    "candidates": {
        "fetched": fetched,
        "rewritten_ready": ready,
        "skipped_safety": skipped,
        "failed": failed_cand,
    },
    "draft_posts": {
        "published": published,
        "failed": failed_posts,
    },
    "llm_usage": {
        "calls": llm_calls,
        "errors": llm_errors,
    },
}

with open(out, "w") as f:
    json.dump(stats, f, indent=2, ensure_ascii=False)

print(f"[mf-daily-stats] wrote {out}")
print(json.dumps(stats, indent=2, ensure_ascii=False))
PYEOF

# Ротация: удалить старше 30 дней
find "$OUT_DIR" -name "*.json" -mtime +30 -delete 2>/dev/null || true
