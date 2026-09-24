#!/usr/bin/env bash
# Pull latest main and rebuild/restart Hermes. Run on the VPS:
#   cd /docker/hermes-app && bash scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
# --build is required: plain `up -d` keeps running the old image.
if [ -n "$(docker compose ps -q cloudflared 2>/dev/null)" ]; then
    docker compose --profile tunnel up -d --build
else
    docker compose up -d --build
fi

echo ">> Waiting for health check..."
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8700/health >/dev/null 2>&1; then
        echo ">> Healthy. Running commit: $(git rev-parse --short HEAD)"
        exit 0
    fi
    sleep 2
done
echo "!! App did not become healthy — check: docker compose logs --tail=100 hermes" >&2
exit 1
