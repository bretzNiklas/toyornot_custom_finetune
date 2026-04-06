#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <commit-sha>" >&2
    exit 64
fi

COMMIT_SHA="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_APP_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
APP_DIR="${APP_DIR:-${DEFAULT_APP_DIR}}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env.local}"
MODEL_ROOT="${MODEL_ROOT:-${APP_DIR}/models}"
RUNTIME_ROOT="${RUNTIME_ROOT:-${APP_DIR}/runtime}"
JOBS_DB_PATH="${JOBS_DB_PATH:-${RUNTIME_ROOT}/jobs.sqlite3}"
JOB_SPOOL_DIR="${JOB_SPOOL_DIR:-${RUNTIME_ROOT}/spool}"
JUDGED_IMAGE_ARCHIVE_DIR="${JUDGED_IMAGE_ARCHIVE_DIR:-${RUNTIME_ROOT}/judged-images}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-graffiti-student}"
WORKER_SERVICE_NAME="${WORKER_SERVICE_NAME:-graffiti-student-worker}"
HANDOFF_WORKER_SERVICE_NAME="${HANDOFF_WORKER_SERVICE_NAME:-graffiti-judge-handoff-worker}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:8000/health}"
HEALTHCHECK_MAX_ATTEMPTS="${HEALTHCHECK_MAX_ATTEMPTS:-90}"

can_sudo() {
    sudo -n true >/dev/null 2>&1
}

dump_service_logs() {
    echo "Deployment failed. Recent ${SERVICE_NAME} logs:" >&2
    if can_sudo; then
        sudo systemctl status "${SERVICE_NAME}" --no-pager || true
        sudo systemctl status "${WORKER_SERVICE_NAME}" --no-pager || true
        sudo systemctl status "${HANDOFF_WORKER_SERVICE_NAME}" --no-pager || true
        sudo journalctl -u "${SERVICE_NAME}" -n 100 -l --no-pager || true
        sudo journalctl -u "${WORKER_SERVICE_NAME}" -n 100 -l --no-pager || true
        sudo journalctl -u "${HANDOFF_WORKER_SERVICE_NAME}" -n 100 -l --no-pager || true
    else
        systemctl status "${SERVICE_NAME}" --no-pager || true
        systemctl status "${WORKER_SERVICE_NAME}" --no-pager || true
        systemctl status "${HANDOFF_WORKER_SERVICE_NAME}" --no-pager || true
        journalctl -u "${SERVICE_NAME}" -n 100 -l --no-pager || true
        journalctl -u "${WORKER_SERVICE_NAME}" -n 100 -l --no-pager || true
        journalctl -u "${HANDOFF_WORKER_SERVICE_NAME}" -n 100 -l --no-pager || true
    fi
}

restart_service() {
    local target_service="$1"
    if can_sudo; then
        sudo systemctl daemon-reload
        sudo systemctl restart "${target_service}"
        sudo systemctl is-active --quiet "${target_service}"
        return
    fi

    local main_pid
    main_pid="$(systemctl show -p MainPID --value "${target_service}")"
    if [[ -z "${main_pid}" || "${main_pid}" == "0" ]]; then
        echo "Unable to determine MainPID for ${target_service} without sudo." >&2
        exit 1
    fi

    kill -TERM "${main_pid}"

    for _ in {1..30}; do
        sleep 1
        if systemctl is-active --quiet "${target_service}"; then
            local new_pid
            new_pid="$(systemctl show -p MainPID --value "${target_service}")"
            if [[ -n "${new_pid}" && "${new_pid}" != "0" && "${new_pid}" != "${main_pid}" ]]; then
                return
            fi
        fi
    done

    echo "Service ${target_service} did not restart cleanly after SIGTERM." >&2
    exit 1
}

trap dump_service_logs ERR

if [[ ! -d "${APP_DIR}/.git" ]]; then
    echo "Expected a git checkout at ${APP_DIR}" >&2
    exit 1
fi

if [[ ! -r "${ENV_FILE}" ]]; then
    echo "Expected a readable env file at ${ENV_FILE}" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${AUTH_TOKEN:?AUTH_TOKEN must be set in ${ENV_FILE}}"
: "${MODEL_REPO_ID:?MODEL_REPO_ID must be set in ${ENV_FILE}}"
: "${MODEL_VERSION:?MODEL_VERSION must be set in ${ENV_FILE}}"
MODEL_REVISION="${MODEL_REVISION:-main}"
MODEL_DIR="${MODEL_DIR:-${MODEL_ROOT}/${MODEL_VERSION}}"
export MODEL_DIR MODEL_REPO_ID MODEL_REVISION MODEL_VERSION HF_TOKEN

mkdir -p "${RUNTIME_ROOT}" "${JOB_SPOOL_DIR}" "${JUDGED_IMAGE_ARCHIVE_DIR}"

mkdir -p "${MODEL_ROOT}"

git -C "${APP_DIR}" fetch --prune "${GIT_REMOTE}"
git -C "${APP_DIR}" checkout --force "${COMMIT_SHA}"
git -C "${APP_DIR}" rev-parse --verify "${COMMIT_SHA}^{commit}" >/dev/null

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements-serve.txt"
if [[ -n "${HF_TOKEN:-}" || -f "${MODEL_DIR}/.hf-model-source.json" ]]; then
    model_sync_args=(
        --repo-id "${MODEL_REPO_ID}"
        --revision "${MODEL_REVISION}"
        --target-dir "${MODEL_DIR}"
    )
    if [[ -n "${HF_TOKEN:-}" ]]; then
        model_sync_args+=(--hf-token "${HF_TOKEN}")
    fi
    "${VENV_DIR}/bin/python" "${APP_DIR}/deploy/ubuntu/sync_model_artifact.py" \
        "${model_sync_args[@]}"
else
    echo "Skipping model sync because HF_TOKEN is not set and ${MODEL_DIR}/.hf-model-source.json is missing." >&2
fi

if can_sudo; then
    sudo cp "${APP_DIR}/deploy/ubuntu/graffiti-student.service" /etc/systemd/system/graffiti-student.service
    sudo cp "${APP_DIR}/deploy/ubuntu/graffiti-student-worker.service" /etc/systemd/system/graffiti-student-worker.service
    sudo cp "${APP_DIR}/deploy/ubuntu/graffiti-judge-handoff-worker.service" /etc/systemd/system/graffiti-judge-handoff-worker.service
    sudo cp "${APP_DIR}/deploy/ubuntu/nginx-graffiti-student.conf" /etc/nginx/sites-available/graffiti-student
    sudo ln -sf /etc/nginx/sites-available/graffiti-student /etc/nginx/sites-enabled/graffiti-student
    sudo nginx -t
    sudo systemctl enable "${SERVICE_NAME}" "${WORKER_SERVICE_NAME}" "${HANDOFF_WORKER_SERVICE_NAME}" >/dev/null
    sudo systemctl reload nginx
fi

restart_service "${SERVICE_NAME}"
restart_service "${WORKER_SERVICE_NAME}"
restart_service "${HANDOFF_WORKER_SERVICE_NAME}"

AUTH_TOKEN="${AUTH_TOKEN}" HEALTHCHECK_URL="${HEALTHCHECK_URL}" "${VENV_DIR}/bin/python" - <<'PY'
import json
import os
import time
import urllib.request
import urllib.error

request = urllib.request.Request(
    os.environ["HEALTHCHECK_URL"],
    headers={"Authorization": f"Bearer {os.environ['AUTH_TOKEN']}"},
)

last_error = None
max_attempts = int(os.environ.get("HEALTHCHECK_MAX_ATTEMPTS", "90"))
for _ in range(max_attempts):
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if payload.get("status") != "ok":
            raise SystemExit(f"Health check failed: {payload}")
        print(json.dumps(payload))
        raise SystemExit(0)
    except (urllib.error.URLError, TimeoutError) as exc:
        last_error = exc
        time.sleep(1)

raise SystemExit(f"Health check did not succeed after {max_attempts} retries: {last_error}")
PY
