#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${COEIROINK_CACHE_ROOT:-${RUNNER_TEMP:-/tmp}/coeiroink-runtime}"
APP_ROOT="$RUNTIME_DIR/app"
APP_URL="https://www.dropbox.com/scl/fi/ksqnfq7kzk63mvh1y0gbz/COEIROINK_LINUX_CPU_v.2.13.0.zip?rlkey=hr26irdl8vdt1wwpknezq5rct&st=omsuel3b&dl=1"
LILIN_URL="https://www.dropbox.com/s/wl14g0vwr23kjk6/cb11bdbd-78fc-4f16-b528-a400bae1782d.zip?dl=1"
LILIN_UUID="cb11bdbd-78fc-4f16-b528-a400bae1782d"

mkdir -p "$APP_ROOT"
engine_bin="$(find "$APP_ROOT" -type f -path '*/engine/engine' -print -quit || true)"
if [[ -z "$engine_bin" ]]; then
  app_zip="${RUNNER_TEMP:-/tmp}/coeiroink-linux-cpu-v2.13.0.zip"
  curl --fail --location --retry 3 --retry-all-errors "$APP_URL" --output "$app_zip"
  unzip -q "$app_zip" -d "$APP_ROOT"
  rm -f "$app_zip"
  engine_bin="$(find "$APP_ROOT" -type f -path '*/engine/engine' -print -quit || true)"
fi
if [[ -z "$engine_bin" ]]; then
  echo "COEIROINK Linux CPU版の engine/engine が見つかりません" >&2
  exit 1
fi
chmod +x "$engine_bin"
app_dir="${engine_bin%/engine/engine}"
speaker_info="$app_dir/speaker_info"
mkdir -p "$speaker_info"

has_lilin_metadata() {
  local metadata_file
  while IFS= read -r -d '' metadata_file; do
    if grep -q "$LILIN_UUID" "$metadata_file"; then
      return 0
    fi
  done < <(find "$speaker_info" -type f -name metas.json -print0)
  return 1
}

if ! has_lilin_metadata; then
  voice_zip="${RUNNER_TEMP:-/tmp}/coeiroink-lilin-2.1.0.zip"
  curl --fail --location --retry 3 --retry-all-errors "$LILIN_URL" --output "$voice_zip"
  unzip -q "$voice_zip" -d "$speaker_info"
  rm -f "$voice_zip"
fi

if ! has_lilin_metadata; then
  echo "リリンちゃんの音声モデルを COEIROINK に配置できませんでした" >&2
  exit 1
fi

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "COEIROINK_ENGINE_PATH=$engine_bin" >> "$GITHUB_ENV"
fi
printf 'COEIROINK runtime ready: %s\n' "$app_dir"
