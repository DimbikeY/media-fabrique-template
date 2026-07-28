#!/usr/bin/env bash
# mf-morning-report.sh — thin wrapper around `python -m morning_report`.
#
# The morning_report module already handles DB queries, HTML formatting,
# and dual TG pushes (morning + ideas topics). The shell wrapper exists
# because cron historically can't `cd` into the repo reliably and we
# wanted a stable binary at /opt/<deploy-user>-ops/bin/.
#
# DD 2026-07-20 09:14 MSK: this file is committed to the repo under
# deploy/mf-morning-report.sh. Deploy workflow:
#   1. Edit the repo copy
#   2. git push
#   3. ssh <vps-host> 'cp /opt/<deploy-user>/media-fabrique-template/deploy/mf-morning-report.sh /opt/<deploy-user>-ops/bin/'
#
# We removed the old `sudo -u <deploy-user> .venv/bin/python -m morning_report --json`
# branch because:
#   - cron already launches the script as user=<deploy-user> (sudo not needed)
#   - --json was never a real arg of morning_report.py; it tried to
#     call morning_report for a JSON dump, but the module only does
#     the full HTML render + TG push.
#
# Pipeline (delegated to morning_report.py):
#   1. Aggregate last 24h from news_memory.db (publish/skip/token/feedback).
#   2. Format two HTML messages: morning + ideas.
#   3. Push to TG topics via tg_bridge (morning → #morning-report,
#      ideas → #ideas). Best-effort.
#
# Run by /etc/cron.d/mf-morning-report daily at 07:00 MSK.

set -euo pipefail

LOGFILE="/var/log/<project>/mf-morning.log"
mkdir -p /var/log/<project>

_ts() { date +%Y-%m-%d_%H:%M:%S; }
log() { echo "[$(_ts)] $*" >> "$LOGFILE"; }

cd /opt/<deploy-user>/media-fabrique-template
log "Generating morning report (--since 24h)..."
if .venv/bin/python -m morning_report --since 24h >> "$LOGFILE" 2>&1; then
    log "OK: morning_report completed"
else
    rc=$?
    log "ERROR: morning_report exited with rc=$rc"
    exit $rc
fi
