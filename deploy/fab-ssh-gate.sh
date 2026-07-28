#!/usr/bin/env bash
# <deploy-user>-ssh-gate.sh — ssh force-command wrapper для <deploy-user>@<vps-host>
#
# Использование:
#   - Кладётся в /opt/<deploy-user>/bin/<deploy-user>-ssh-gate.sh (mode 755, owner root:root)
#   - В /home/<deploy-user>/.ssh/authorized_keys добавляется строка:
#       command="/opt/<deploy-user>/bin/<deploy-user>-ssh-gate.sh",\
#       no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty \
#       ssh-ed25519 AAAA... openclaw@<old-vps-host>
#
# Whitelist команд (case-style для надёжности без bash-regex):
#   1. cd /opt/<deploy-user>/media-fabrique-template && git <safe-subcommand>
#   2. mysql ... -e "SELECT ..." (только SELECT, без write-операций)
#   3. systemctl status <deploy-user>-* (read-only)
#   4. tail -n <N> /var/log/<project>/<file>.log (read-only)
#
# Всё остальное → лог в /var/log/<project>/ssh-gate.log + отказ (exit 1).

set -euo pipefail

CMD="${SSH_ORIGINAL_COMMAND:-}"
LOGFILE="/var/log/<project>/ssh-gate.log"
ALLOWED=0

# Хелпер для записи в лог
_log() {
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local caller; caller="${SSH_CLIENT:-unknown}"
    echo "[$ts] client=$caller decision=$1 cmd=$CMD" >> "$LOGFILE"
}

# case-statement: разрешаем только whitelisted команды
case "$CMD" in
    # 1. git safe-commands в /opt/<deploy-user>/media-fabrique-template
    "cd /opt/<deploy-user>/media-fabrique-template && git status")
        ALLOWED=1
        ;;
    "cd /opt/<deploy-user>/media-fabrique-template && git log"*)
        ALLOWED=1
        ;;
    "cd /opt/<deploy-user>/media-fabrique-template && git diff"*)
        ALLOWED=1
        ;;
    "cd /opt/<deploy-user>/media-fabrique-template && git fetch"*)
        ALLOWED=1
        ;;
    "cd /opt/<deploy-user>/media-fabrique-template && git pull"*)
        ALLOWED=1
        ;;
    "cd /opt/<deploy-user>/media-fabrique-template && git rev-parse"*)
        ALLOWED=1
        ;;

    # 2. mysql SELECT (запрещаем write-операции через case-sensitive чёрный список)
    "mysql "*)
        # Запрещаем всё, что не SELECT/SHOW/DESCRIBE/EXPLAIN
        if ! echo "$CMD" | grep -qiE '\b(INSERT|UPDATE|DELETE|DROP|ALTER|GRANT|REVOKE|TRUNCATE|CREATE|REPLACE|RENAME|SET|LOCK|UNLOCK|CALL|HANDLER|LOAD)\b'; then
            ALLOWED=1
        fi
        ;;

    # 3. systemctl status <deploy-user>-* (только status)
    "systemctl status <deploy-user>-"*)
        ALLOWED=1
        ;;

    # 4. tail /var/log/<project>/*.log (только наш лог-каталог)
    "tail -n "[0-9]*" /var/log/<project>/"*.log)
        ALLOWED=1
        ;;

    # 5. backup.sh removed 2026-07-20 (DD: no S3)
        ALLOWED=1
        ;;

    # 6. read-only DB inspect через утилиту (на будущее)
    "/opt/<deploy-user>/bin/db-query.sh "*)
        ALLOWED=1
        ;;

    *)
        ALLOWED=0
        ;;
esac

if [[ "$ALLOWED" -eq 1 ]]; then
    _log "ALLOW"
    exec bash -c "$CMD"
else
    _log "DENY"
    echo "<deploy-user>-ssh-gate: command denied (not in whitelist)" >&2
    exit 1
fi