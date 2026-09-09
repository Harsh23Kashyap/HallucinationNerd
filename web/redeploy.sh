#!/usr/bin/env bash
# Redeploy the HallucinationNerd backend on the EC2 server.
# Usage (on the server):  ~/HallucinationNerd/web/redeploy.sh
#
# Pulls the latest code from GitHub and restarts the FastAPI service.
# Safe to run repeatedly.

set -e

REPO_DIR="$HOME/HallucinationNerd"
SERVICE="hallucinationnerd"

echo "==> Pulling latest code..."
cd "$REPO_DIR"
git pull --ff-only

echo "==> Installing any new dependencies..."
# uses the existing venv created with uv/python 3.12
"$REPO_DIR/web/venv/bin/python" -m pip install -q -r "$REPO_DIR/web/requirements.txt" || true

echo "==> Restarting the service..."
sudo systemctl restart "$SERVICE"
sleep 2
sudo systemctl --no-pager --lines=0 status "$SERVICE" | head -3

echo "==> Health check..."
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ || echo "000")
if [ "$code" = "200" ]; then
  echo "==> OK: backend is up (HTTP 200)."
else
  echo "==> WARNING: health check returned $code. Check: sudo journalctl -u $SERVICE -n 50"
fi
