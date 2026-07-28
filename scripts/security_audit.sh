#!/usr/bin/env bash
# security_audit.sh — pre-deploy security check for media-fabrique-template
#
# Прогоняет:
#   1. pip-audit на prod venv (известные CVE в зависимостях)
#   2. grep по .py файлам на запрещённые паттерны (eval/exec/shell=True/pickle)
#   3. grep кода на захардкоженные секреты
#   4. Проверка что .env в .gitignore
#   5. Проверка что HTTP-запросы не идут без timeout
#
# Usage:
#   ./.venv/bin/python -m venv_check  # опционально
#   ./scripts/security_audit.sh
#
# Exit codes:
#   0 — всё чисто
#   1 — найдены проблемы (см. вывод)

set -uo pipefail

cd "$(dirname "$0")/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

FAIL=0
WARN=0

fail()   { echo -e "${RED}✗${NC} $1"; FAIL=$((FAIL + 1)); }
warn()   { echo -e "${YELLOW}⚠${NC} $1"; WARN=$((WARN + 1)); }
ok()     { echo -e "${GREEN}✓${NC} $1"; }

echo "==> Security Audit (media-fabrique-template)"
echo

# --- 1. pip-audit ---
echo "[1] pip-audit (зависимости)..."
if [[ -d .venv ]]; then
    if .venv/bin/pip-audit >/dev/null 2>&1; then
        ok "pip-audit: 0 CVE"
    else
        fail "pip-audit: найдены уязвимости, см. .venv/bin/pip-audit"
        .venv/bin/pip-audit 2>&1 | head -20
    fi
else
    warn ".venv не найден, pip-audit пропущен"
fi

echo

# --- 2. Запрещённые конструкции в коде ---
echo "[2] Запрещённые паттерны в Python-коде..."
BAD_PATTERNS=(
    "eval("
    "exec("
    "pickle.loads"
    "shell=True"
    "subprocess.call"   # небезопасно без списка аргументов
    "input("            # опасно в prod
)

FOUND_BAD=0
for pat in "${BAD_PATTERNS[@]}"; do
    # Исключаем тесты и скрипты вне prod
    if grep -rn --include="*.py" --exclude-dir=".venv" --exclude-dir="tests" --exclude-dir="scripts" \
        "$pat" . 2>/dev/null | grep -v "security_audit" | head -3 | grep -q .; then
        matches=$(grep -rn --include="*.py" --exclude-dir=".venv" --exclude-dir="tests" --exclude-dir="scripts" \
            "$pat" . 2>/dev/null | grep -v "security_audit" | wc -l | tr -d ' ')
        if [[ "$matches" -gt 0 ]]; then
            warn "Найдено '$pat' ($matches раз) — проверь что это намеренно"
            FOUND_BAD=$((FOUND_BAD + 1))
        fi
    fi
done

if [[ $FOUND_BAD -eq 0 ]]; then
    ok "Запрещённые паттерны не найдены"
fi

echo

# --- 3. Захардкоженные секреты ---
echo "[3] Захардкоженные секреты..."
SECRET_PATTERNS=(
    'sk-[A-Za-z0-9]{20,}'
    'ghp_[A-Za-z0-9]{20,}'
    'AIza[0-9A-Za-z_-]{35}'
    'AKIA[0-9A-Z]{16}'
    'xox[baprs]-[A-Za-z0-9-]{10,}'
)

FOUND_SECRETS=0
for pat in "${SECRET_PATTERNS[@]}"; do
    if grep -rEn --include="*.py" --include="*.sh" --include="*.md" --exclude-dir=".venv" --exclude-dir=".git" \
        "$pat" . 2>/dev/null | grep -v "security_audit" | head -3 | grep -q .; then
        # Игнорируем .env.example
        matches=$(grep -rEn --include="*.py" --include="*.sh" --include="*.md" --exclude-dir=".venv" --exclude-dir=".git" \
            "$pat" . 2>/dev/null | grep -v "security_audit" | grep -v ".env.example" | wc -l | tr -d ' ')
        if [[ "$matches" -gt 0 ]]; then
            fail "Найдены секретоподобные строки ($matches) — grep -rE '$pat' ."
            FOUND_SECRETS=$((FOUND_SECRETS + 1))
        fi
    fi
done

if [[ $FOUND_SECRETS -eq 0 ]]; then
    ok "Захардкоженные секреты не найдены"
fi

echo

# --- 4. .env в .gitignore ---
echo "[4] .gitignore..."
if [[ -f .gitignore ]]; then
    if grep -q "^\.env$" .gitignore; then
        ok ".env в .gitignore"
    else
        fail ".env НЕ в .gitignore — потенциальная утечка секретов"
    fi
else
    fail ".gitignore не найден"
fi

echo

# --- 5. HTTP без timeout ---
echo "[5] HTTP-запросы без timeout..."
# Ищем requests.get/post без параметра timeout=
if grep -rn --include="*.py" --exclude-dir=".venv" --exclude-dir="tests" \
    -E "(requests|httpx)\.(get|post|put|patch|delete|request)\([^)]*\)" . 2>/dev/null | \
    grep -v "security_audit" | grep -v "timeout=" | head -5 | grep -q .; then
    fail "Найдены HTTP-запросы без timeout= — риск зависания на сетевых ошибках"
    grep -rn --include="*.py" --exclude-dir=".venv" --exclude-dir="tests" \
        -E "(requests|httpx)\.(get|post|put|patch|delete|request)\([^)]*\)" . 2>/dev/null | \
        grep -v "security_audit" | grep -v "timeout=" | head -3
else
    ok "Все HTTP-запросы с timeout="
fi

echo

# --- 6. .env.example sanity ---
echo "[6] .env.example..."
if [[ -f .env.example ]]; then
    # Не должно быть реальных секретов в .env.example (только placeholders)
    if grep -E "(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,})" .env.example 2>/dev/null; then
        fail ".env.example содержит реальные секреты!"
    else
        ok ".env.example — только placeholders"
    fi
else
    warn ".env.example отсутствует"
fi

echo

# --- Итог ---
echo "==> Итог: $FAIL ошибок, $WARN предупреждений"
if [[ $FAIL -gt 0 ]]; then
    echo "АУДИТ НЕ ПРОЙДЕН. Исправьте ошибки перед деплоем."
    exit 1
else
    echo "Аудит пройден."
    exit 0
fi