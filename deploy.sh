#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${STORE_RUN_REPO_URL:-https://github.com/thaynenaidoo/store-run.git}"
APP_DIR="${STORE_RUN_APP_DIR:-}"
DATA_DIR="${STORE_RUN_DATA_DIR:-}"
ENV_FILE="${STORE_RUN_ENV_FILE:-/etc/store-run.env}"
SERVICE_NAME="store-run"
SERVICE_USER="store-run"
SERVICE_GROUP="store-run"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

fail() {
  printf 'Store Run deployment failed: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root (for example: sudo bash deploy.sh)."
command -v apt-get >/dev/null 2>&1 || fail "this bootstrap script supports Debian and Ubuntu LXC containers."
command -v systemctl >/dev/null 2>&1 || fail "systemd is required in the LXC container."

if [[ -z "$APP_DIR" ]]; then
  if [[ -f "${SCRIPT_DIR}/app.py" ]]; then
    APP_DIR="$SCRIPT_DIR"
  else
    APP_DIR="/opt/store-run"
  fi
fi

if [[ -z "$DATA_DIR" && -f "$ENV_FILE" ]]; then
  configured_data_dir="$(sed -n 's/^DATA_DIR=//p' "$ENV_FILE" | tail -n 1 | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  if [[ -n "$configured_data_dir" ]]; then
    DATA_DIR="$configured_data_dir"
  fi
fi
DATA_DIR="${DATA_DIR:-/var/lib/store-run}"
DATA_DIR="$(printf '%s' "$DATA_DIR" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"

[[ "$APP_DIR" != *[[:space:]]* ]] || fail "STORE_RUN_APP_DIR contains whitespace: $(printf '%q' "$APP_DIR"). Choose a path without spaces."
[[ "$DATA_DIR" != *[[:space:]]* ]] || fail "resolved STORE_RUN_DATA_DIR contains whitespace: $(printf '%q' "$DATA_DIR"). Remove spaces from DATA_DIR in ${ENV_FILE} or set STORE_RUN_DATA_DIR to a path without spaces."
printf 'Using app directory: %s\nUsing data directory: %s\n' "$APP_DIR" "$DATA_DIR"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv ca-certificates

if [[ ! -f "${APP_DIR}/app.py" ]]; then
  if [[ -e "$APP_DIR" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail "${APP_DIR} exists but is not a Store Run checkout. Set STORE_RUN_APP_DIR to an empty path or a valid checkout."
  fi
  install -d -m 0755 "$(dirname -- "$APP_DIR")"
  git clone -- "$REPO_URL" "$APP_DIR"
fi

[[ -f "${APP_DIR}/requirements.txt" ]] || fail "requirements.txt is missing from ${APP_DIR}."
[[ -f "${APP_DIR}/deploy/store-run.service.example" ]] || fail "the Store Run service template is missing from ${APP_DIR}."

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$DATA_DIR"

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
else
  "${APP_DIR}/.venv/bin/python" -m venv --upgrade "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  install -d -m 0755 "$(dirname -- "$ENV_FILE")"
  umask 077
  cat > "$ENV_FILE" <<EOF
DATA_DIR=${DATA_DIR}
COOKIE_SECURE=0
EOF
  chmod 0600 "$ENV_FILE"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Store Run household shopping app
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python -m waitress --listen=0.0.0.0:8000 app:app
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

if ! "${APP_DIR}/.venv/bin/python" -c 'import time, urllib.request; url="http://127.0.0.1:8000/healthz"; ok=False
for _ in range(20):
    try:
        ok=urllib.request.urlopen(url, timeout=2).status == 200
        if ok: break
    except Exception:
        time.sleep(1)
raise SystemExit(0 if ok else 1)'; then
  systemctl --no-pager --full status "$SERVICE_NAME" >&2 || true
  fail "the service did not pass its health check; inspect with: journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
fi

printf 'Store Run is deployed and healthy.\n'
printf 'App directory: %s\nData directory: %s\n' "$APP_DIR" "$DATA_DIR"
printf 'Open http://<LXC-IP>:8000 from your LAN.\n'
printf 'For app updates: git -C %q pull --ff-only && bash %q\n' "$APP_DIR" "${APP_DIR}/deploy.sh"
