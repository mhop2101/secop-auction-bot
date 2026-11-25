#!/bin/bash
# Install or update the web app as a systemd service on the bot server.
#
# Run from the repository on the server:   bash deploy/install.sh
#
# Idempotent. On the first run it also creates
#   secrets/tls/cert.pem, key.pem    a self-signed certificate for the server's public IP
#   secrets/webapp_users.json        the login (PBKDF2 hash)
#   secrets/webapp_login.txt         the same login in plain text, mode 0600, for the operator
# and never overwrites them afterwards. Delete a file to have it regenerated.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="$(id -un)"
PY="$APP_DIR/.venv/bin/python"
cd "$APP_DIR"
umask 077
mkdir -p secrets/tls webjobs

if [ ! -s secrets/tls/cert.pem ] || [ ! -s secrets/tls/key.pem ]; then
    IP="$(curl -s --max-time 5 https://checkip.amazonaws.com || true)"
    IP="${IP:-127.0.0.1}"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
        -keyout secrets/tls/key.pem -out secrets/tls/cert.pem \
        -subj "/CN=$IP" -addext "subjectAltName=IP:$IP" >/dev/null 2>&1
    echo "created a self-signed certificate for $IP"
fi

if [ ! -s secrets/webapp_users.json ]; then
    USERNAME="operador"
    PASSWORD="$("$PY" -c 'from secop_web.auth import new_password; print(new_password())')"
    printf '%s\n' "$PASSWORD" > secrets/.pw.tmp
    "$PY" -m secop_web make-user --users secrets/webapp_users.json \
        --username "$USERNAME" --password-file secrets/.pw.tmp >/dev/null
    printf 'user: %s\npassword: %s\n' "$USERNAME" "$PASSWORD" > secrets/webapp_login.txt
    rm -f secrets/.pw.tmp
    echo "created the login; it is in secrets/webapp_login.txt"
fi
chmod 600 secrets/webapp_users.json secrets/webapp_login.txt secrets/tls/*.pem

sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@APP_USER@#$APP_USER#g" deploy/secop-web.service \
    | sudo tee /etc/systemd/system/secop-web.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable secop-web.service >/dev/null 2>&1
sudo systemctl restart secop-web.service
sleep 2
systemctl --no-pager --lines=5 status secop-web.service | head -12
