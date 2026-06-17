#!/usr/bin/env bash
# Route hermes.growgig.tech -> Hermes app via the host nginx reverse proxy.
# HTTPS is handled at the edge by Cloudflare (origin stays plain HTTP on :80).
# Run on the VPS:  cd /docker/hermes-app && git pull && bash scripts/setup-nginx.sh
set -e

HOST=hermes.growgig.tech
APPDIR=/docker/hermes-app

echo ">> Recreating container on fixed localhost port 8700..."
cd "$APPDIR"
docker compose up -d

echo ">> Writing nginx site for $HOST..."
cat > /etc/nginx/conf.d/hermes.conf <<'NGINX'
server {
    listen 80;
    server_name hermes.growgig.tech;
    location / {
        proxy_pass http://127.0.0.1:8700;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX
nginx -t && systemctl reload nginx

echo "=== DONE ==="
echo ">> nginx now routes hermes.growgig.tech -> 127.0.0.1:8700 (Cloudflare provides HTTPS)"
curl -s -o /dev/null -w ">> local app health: HTTP %{http_code}\n" http://127.0.0.1:8700/health || true
