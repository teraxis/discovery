#!/usr/bin/env sh
# Встановлення discovery (macOS / Linux). Повторний запуск безпечний.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
NLM_HOME="${NLM_HOME:-$HOME/.nlm-pipeline}"
mkdir -p "$NLM_HOME/prompts" "$NLM_HOME/types"

PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "✗ Потрібен Python 3.10+ (brew install python@3.12 / apt install python3.12-venv)"; exit 1; }
echo "✓ Python: $PY"

[ -x "$NLM_HOME/.venv/bin/python" ] || "$PY" -m venv "$NLM_HOME/.venv"
"$NLM_HOME/.venv/bin/python" -m pip install --upgrade pip -q
"$NLM_HOME/.venv/bin/python" -m pip install -q -r "$HERE/requirements.txt"
echo "✓ Залежності встановлено"

if ! command -v google-chrome >/dev/null 2>&1 && [ ! -d "/Applications/Google Chrome.app" ]; then
  "$NLM_HOME/.venv/bin/python" -m playwright install chromium
fi
echo "✓ Браузер для входу готовий"

[ -f "$NLM_HOME/config.yaml" ] || cp "$HERE/config.example.yaml" "$NLM_HOME/config.yaml"
chmod +x "$HERE/nlm"
echo "$HERE" > "$NLM_HOME/installed_from.txt"
echo
echo "Готово. Далі:"
echo "  1) Вхід у NotebookLM (робить людина):  \"$HERE/nlm\" login"
echo "  2) Перевірка:                          \"$HERE/nlm\" doctor"
