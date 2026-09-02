#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV_BIN="${IDEA_RESEARCH_VENV_BIN:-${PROJECT_DIR}/.venv/bin}"
CLI="${VENV_BIN}/idea-research"
PYTHON="${VENV_BIN}/python"
LOCK_DIR="${PROJECT_DIR}/.runtime"
LOCK_FILE="${LOCK_DIR}/feed-update.lock"
BRANCH="${IDEA_RESEARCH_GIT_BRANCH:-main}"
ROLLING_HOURS="${IDEA_RESEARCH_ROLLING_HOURS:-24}"

log() {
  printf '%s %s\n' "$(TZ=Asia/Shanghai date '+%F %T %Z')" "$*"
}

publish_generated_files() {
  local commit_message="$1"
  local attempt
  local path
  local publish_paths=(
    data/feeds/latest.json
    data/media/reddit
    data/prices/rolling_prices.json
    data/stock_universe/stock_pool.json
    data/stock_universe/saas_pool.json
  )

  for path in "${publish_paths[@]}"; do
    if [[ -e "${path}" ]] || git ls-files -- "${path}" | grep -q .; then
      git add -A -- "${path}"
    fi
  done

  if git diff --cached --quiet; then
    log "No generated feed changes to publish."
    return 0
  fi

  git commit -m "${commit_message}"
  for attempt in 1 2 3; do
    if git push origin "${BRANCH}"; then
      return 0
    fi
    if [[ "${attempt}" -lt 3 ]]; then
      log "Push attempt ${attempt} failed; retrying in 30 seconds."
      sleep 30
    fi
  done
  log "Push failed after three attempts."
  return 1
}

cd "${PROJECT_DIR}"
mkdir -p "${LOCK_DIR}"

if ! command -v flock >/dev/null 2>&1; then
  log "flock is required (install the util-linux package)."
  exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "Another feed update is already running; exiting without overlap."
  exit 0
fi

if [[ ! -f .env ]]; then
  log "Missing ${PROJECT_DIR}/.env; refusing to collect without central credentials."
  exit 1
fi
if [[ ! -x "${CLI}" || ! -x "${PYTHON}" ]]; then
  log "Missing project virtual environment; create .venv and install the package first."
  exit 1
fi
if ! git diff --cached --quiet; then
  log "The index already contains staged changes; refusing to include them in an automated commit."
  exit 1
fi
current_branch="$(git branch --show-current)"
if [[ "${current_branch}" != "${BRANCH}" ]]; then
  log "Expected branch ${BRANCH}, found ${current_branch}; refusing to publish."
  exit 1
fi

log "Updating ${BRANCH} before collection."
git pull --ff-only origin "${BRANCH}"

"${CLI}" doctor

# Publish public/fast sources first. X can wait through rate-limit cooldowns,
# so it deliberately runs and publishes in a second phase.
log "Collecting Newsletter, Reddit, and US close data from the past ${ROLLING_HOURS} hours."
"${CLI}" collect \
  --rolling-hours "${ROLLING_HOURS}" \
  --pipeline substack \
  --pipeline reddit \
  --pipeline prices
"${PYTHON}" -m json.tool data/feeds/latest.json >/dev/null
feed_date="$(TZ=Asia/Shanghai date +%F)"
publish_generated_files "Update non-X feeds for ${feed_date}"

if [[ "${IDEA_RESEARCH_SKIP_X:-0}" == "1" ]]; then
  log "IDEA_RESEARCH_SKIP_X=1; non-X feed is published and X was skipped."
  exit 0
fi

log "Collecting X in the second phase; cooldown waits cannot delay the non-X publish."
"${CLI}" collect --rolling-hours "${ROLLING_HOURS}" --pipeline x
"${PYTHON}" -m json.tool data/feeds/latest.json >/dev/null
publish_generated_files "Update X feed for ${feed_date}"
log "Daily feed update completed."
