#!/usr/bin/env bash

set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

: "${RUNNER_TOKEN:?RUNNER_TOKEN must be set}"

REPO_URL="${REPO_URL:-https://github.com/bretzNiklas/toyornot_custom_finetune.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
RUNNER_VERSION="${RUNNER_VERSION:-2.333.1}"
SERVICE_USER="${SERVICE_USER:-graffiti}"
SERVICE_GROUP="${SERVICE_GROUP:-graffiti}"
APP_ROOT="${APP_ROOT:-/srv/graffiti-student}"
APP_DIR="${APP_DIR:-${APP_ROOT}/app}"
MODEL_ROOT="${MODEL_ROOT:-${APP_ROOT}/models}"
MODEL_DIR="${MODEL_DIR:-${MODEL_ROOT}/student-v2-dinov2}"
RUNTIME_ROOT="${RUNTIME_ROOT:-${APP_ROOT}/runtime}"
JOBS_DB_PATH="${JOBS_DB_PATH:-${RUNTIME_ROOT}/jobs.sqlite3}"
JOB_SPOOL_DIR="${JOB_SPOOL_DIR:-${RUNTIME_ROOT}/spool}"
VENV_DIR="${VENV_DIR:-${APP_ROOT}/venv}"
ENV_FILE="${ENV_FILE:-/etc/graffiti-student.env}"
OLD_APP_DIR="${OLD_APP_DIR:-/home/niklas/toyornot_custom_finetune}"
OLD_ENV_FILE="${OLD_ENV_FILE:-${OLD_APP_DIR}/.env.local}"
OLD_MODEL_DIR="${OLD_MODEL_DIR:-${OLD_APP_DIR}/models/dinov2_base_224}"
RUNNER_ROOT="${RUNNER_ROOT:-/srv/actions-runner-graffiti}"
RUNNER_ARCHIVE="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_ARCHIVE}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)-graffiti-deploy}"
RUNNER_LABELS="${RUNNER_LABELS:-graffiti-deploy}"
RUNNER_WORKDIR="${RUNNER_WORKDIR:-_work}"
RUNNER_SERVICE_NAME="actions.runner.bretzNiklas-toyornot_custom_finetune.${RUNNER_NAME}.service"

apt-get update
apt-get install -y git python3 python3-venv python3-pip nginx curl tar rsync

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "${APP_ROOT}" --shell /bin/bash "${SERVICE_USER}"
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${APP_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${MODEL_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${RUNTIME_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${JOB_SPOOL_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${RUNNER_ROOT}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
    sudo -u "${SERVICE_USER}" git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${APP_DIR}"
else
    sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" fetch origin
    sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" checkout --force "origin/${REPO_BRANCH}"
fi

if [[ -d "${OLD_MODEL_DIR}" && ! -d "${MODEL_DIR}" ]]; then
    install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${MODEL_DIR}"
    rsync -a "${OLD_MODEL_DIR}/" "${MODEL_DIR}/"
    chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${MODEL_DIR}"
fi

python3 - "${ENV_FILE}" "${OLD_ENV_FILE}" "${MODEL_DIR}" "${MODEL_ROOT}" "${RUNTIME_ROOT}" "${JOBS_DB_PATH}" "${JOB_SPOOL_DIR}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
old_env_file = Path(sys.argv[2])
model_dir = sys.argv[3]
model_root = sys.argv[4]
runtime_root = sys.argv[5]
jobs_db_path = sys.argv[6]
job_spool_dir = sys.argv[7]


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


values = parse_env(old_env_file)
current = parse_env(env_file)
values.update(current)

auth_token = values.get("AUTH_TOKEN") or values.get("GRAFFITI_API_TOKEN")
if not auth_token:
    raise SystemExit("AUTH_TOKEN or GRAFFITI_API_TOKEN must exist in the old env file.")

merged = {
    "AUTH_TOKEN": auth_token,
    "MODEL_REPO_ID": values.get("MODEL_REPO_ID", "qwertzniki/graffiti-student-dinov2-base-224"),
    "MODEL_REVISION": values.get("MODEL_REVISION", "main"),
    "MODEL_VERSION": values.get("MODEL_VERSION", "student-v2-dinov2"),
    "MODEL_ROOT": model_root,
    "MODEL_DIR": model_dir,
    "RUNTIME_ROOT": values.get("RUNTIME_ROOT", runtime_root),
    "JOBS_DB_PATH": values.get("JOBS_DB_PATH", jobs_db_path),
    "JOB_SPOOL_DIR": values.get("JOB_SPOOL_DIR", job_spool_dir),
    "WORKER_CONCURRENCY": values.get("WORKER_CONCURRENCY", "1"),
    "JOB_LEASE_SECONDS": values.get("JOB_LEASE_SECONDS", "30"),
    "MAX_RETRIES": values.get("MAX_RETRIES", "2"),
    "MAX_ESTIMATED_WAIT_SECONDS": values.get("MAX_ESTIMATED_WAIT_SECONDS", "90"),
    "JOB_RETENTION_HOURS": values.get("JOB_RETENTION_HOURS", "24"),
    "WORKER_HEARTBEAT_TIMEOUT_SECONDS": values.get("WORKER_HEARTBEAT_TIMEOUT_SECONDS", "45"),
    "WORKER_HEARTBEAT_INTERVAL_SECONDS": values.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", "5"),
    "WORKER_IDLE_POLL_SECONDS": values.get("WORKER_IDLE_POLL_SECONDS", "0.5"),
    "DEFAULT_PROCESSING_SECONDS": values.get("DEFAULT_PROCESSING_SECONDS", "5.0"),
    "PROCESSING_AVERAGE_WINDOW": values.get("PROCESSING_AVERAGE_WINDOW", "20"),
    "ORPHAN_PAYLOAD_GRACE_SECONDS": values.get("ORPHAN_PAYLOAD_GRACE_SECONDS", "300"),
}

if values.get("HF_TOKEN"):
    merged["HF_TOKEN"] = values["HF_TOKEN"]

env_file.write_text(
    "".join(f"{key}={value}\n" for key, value in merged.items()),
    encoding="utf-8",
)

metadata_path = Path(model_dir) / ".hf-model-source.json"
if Path(model_dir).is_dir() and not metadata_path.exists():
    metadata_path.write_text(
        json.dumps(
            {
                "repo_id": merged["MODEL_REPO_ID"],
                "revision": merged["MODEL_REVISION"],
                "updated_at": "bootstrap-seeded",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
PY

chown root:"${SERVICE_GROUP}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${MODEL_ROOT}"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${RUNTIME_ROOT}"

cp "${APP_DIR}/deploy/ubuntu/graffiti-student.service" /etc/systemd/system/graffiti-student.service
cp "${APP_DIR}/deploy/ubuntu/graffiti-student-worker.service" /etc/systemd/system/graffiti-student-worker.service
cp "${APP_DIR}/deploy/ubuntu/nginx-graffiti-student.conf" /etc/nginx/sites-available/graffiti-student
ln -sf /etc/nginx/sites-available/graffiti-student /etc/nginx/sites-enabled/graffiti-student
nginx -t

cat > /etc/sudoers.d/graffiti-student-deploy <<'EOF'
graffiti ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload, /usr/bin/systemctl restart graffiti-student, /usr/bin/systemctl restart graffiti-student-worker, /usr/bin/systemctl is-active graffiti-student, /usr/bin/systemctl is-active graffiti-student-worker, /usr/bin/systemctl status graffiti-student --no-pager, /usr/bin/systemctl status graffiti-student-worker --no-pager, /usr/bin/journalctl -u graffiti-student -n 100 -l --no-pager, /usr/bin/journalctl -u graffiti-student-worker -n 100 -l --no-pager
EOF
chmod 440 /etc/sudoers.d/graffiti-student-deploy

systemctl daemon-reload
systemctl enable graffiti-student
systemctl enable graffiti-student-worker
systemctl restart graffiti-student
systemctl restart graffiti-student-worker
systemctl reload nginx

if [[ ! -f "${RUNNER_ROOT}/.runner" ]]; then
    sudo -u "${SERVICE_USER}" bash -lc "
        set -Eeuo pipefail
        cd '${RUNNER_ROOT}'
        curl -L -o '${RUNNER_ARCHIVE}' '${RUNNER_URL}'
        tar xzf '${RUNNER_ARCHIVE}'
        ./config.sh --unattended \
            --url 'https://github.com/bretzNiklas/toyornot_custom_finetune' \
            --token '${RUNNER_TOKEN}' \
            --name '${RUNNER_NAME}' \
            --labels '${RUNNER_LABELS}' \
            --work '${RUNNER_WORKDIR}' \
            --replace
    "
else
    echo "Runner already configured at ${RUNNER_ROOT}."
fi

if [[ -f "/etc/systemd/system/${RUNNER_SERVICE_NAME}" ]]; then
    if grep -Fq "/home/niklas" "/etc/systemd/system/${RUNNER_SERVICE_NAME}"; then
        systemctl stop "${RUNNER_SERVICE_NAME}" || true
        rm -f "/etc/systemd/system/${RUNNER_SERVICE_NAME}"
        systemctl daemon-reload
    fi
fi

(
    cd "${RUNNER_ROOT}"
    ./svc.sh install "${SERVICE_USER}" || true
    ./svc.sh start
)

sudo -u "${SERVICE_USER}" env \
    APP_DIR="${APP_DIR}" \
    VENV_DIR="${VENV_DIR}" \
    ENV_FILE="${ENV_FILE}" \
    MODEL_ROOT="${MODEL_ROOT}" \
    bash "${APP_DIR}/deploy/ubuntu/deploy_remote.sh" "$(sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" rev-parse HEAD)"

echo "Clean bootstrap completed."
