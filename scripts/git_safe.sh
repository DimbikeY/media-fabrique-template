#!/usr/bin/env bash
# Pre-commit safety check: refuse to commit if common secret patterns appear
# in staged files (other than .env.example which is allowed to mention them
# as placeholder names).
#
# Usage:
#   ./scripts/git_safe.sh

set -euo pipefail

PATTERNS=(
  'sk-[A-Za-z0-9]{20,}'        # OpenAI / OpenRouter
  'ghp_[A-Za-z0-9]{20,}'       # GitHub PAT
  'github_pat_[A-Za-z0-9_]{20,}'
  'xox[baprs]-[A-Za-z0-9-]{10,}' # Slack
  'AKIA[0-9A-Z]{16}'           # AWS access key
  'AIza[0-9A-Za-z_-]{35}'      # Google API key
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)

# Patterns allowed only in .env.example as empty values (key names are fine)
ALLOWED_FILES='(^|/)\.env\.example$|(^|/)scripts/git_safe\.sh$|(^|/)EXPORT\.md$'

FOUND=0
STAGED=$(git diff --cached --name-only --diff-filter=ACM | grep -v -E "$ALLOWED_FILES" || true)

for file in $STAGED; do
  if [[ ! -f "$file" ]]; then continue; fi
  for p in "${PATTERNS[@]}"; do
    if grep -EnH "$p" "$file" >/dev/null 2>&1; then
      echo "REFUSE: $file contains pattern $p" >&2
      FOUND=1
    fi
  done
done

if [[ $FOUND -eq 1 ]]; then
  echo
  echo "Unstage the file, remove the secret, replace with an env var, then retry." >&2
  exit 1
fi

# Soft check for obvious key names with values (best-effort)
SOFT=$(git diff --cached -U0 -- . ':!*.example' ':!*.md' ':!*.sh' \
  | grep -E '^\+.*(api[_-]?key|token|secret|password)\s*=\s*["'"'"']?[A-Za-z0-9_\-]{12,}' || true)
if [[ -n "$SOFT" ]]; then
  echo "WARNING: looks like a real secret in staged content:" >&2
  echo "$SOFT" | head >&2
  echo "Aborting. Move the value to .env and reference via os.getenv()." >&2
  exit 2
fi

echo "git_safe: ok"