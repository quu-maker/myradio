#!/usr/bin/env bash
set -euo pipefail

ENGINE_BIN="${COEIROINK_ENGINE_PATH:-}"
if [[ -z "$ENGINE_BIN" || ! -x "$ENGINE_BIN" ]]; then
  echo "COEIROINK_ENGINE_PATH が設定されていません" >&2
  exit 1
fi

APP_DIR="${ENGINE_BIN%/engine/engine}"
LOG_PATH="${RUNNER_TEMP:-/tmp}/coeiroink-engine.log"
API_URL="${COEIROINK_API_URL:-http://127.0.0.1:50032}"
cd "$APP_DIR"
nohup "$ENGINE_BIN" > "$LOG_PATH" 2>&1 </dev/null &
engine_pid=$!

for attempt in $(seq 1 120); do
  if curl --silent --fail "$API_URL/docs" >/dev/null; then
    if [[ -n "${GITHUB_ENV:-}" ]]; then
      echo "COEIROINK_API_URL=$API_URL" >> "$GITHUB_ENV"
    fi
    echo "COEIROINK local engine is ready"
    exit 0
  fi
  if ! kill -0 "$engine_pid" 2>/dev/null; then
    cat "$LOG_PATH" >&2
    echo "COEIROINK engine stopped during startup" >&2
    exit 1
  fi
  sleep 1
done

cat "$LOG_PATH" >&2
echo "COEIROINK engine did not start within 120 seconds" >&2
exit 1
