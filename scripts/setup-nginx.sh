#!/usr/bin/env bash
# Configure the host nginx reverse proxy + Let's Encrypt HTTPS for the Hermes app.
# Run on the VPS:  cd /docker/hermes-app && git pull && bash scripts/setup-nginx.sh
set -e

HOST=hermes.growgig.tech
EMAIL=anipe2811@gmail.com
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
echo ">> HTTP proxy ready: http://$HOST"

echo ">> Setting up HTTPS via Let's Encrypt..."
if ! command -v certbot >/dev/null 2>&1; then
  apt-get update -y -qq && apt-get install -y -qq certbot python3-certbot-nginx
fi
if certbot --nginx -d "$HOST" --non-interactive --agree-tos -m "$EMAIL" --redirect; then
  echo ">> HTTPS ready: https://$HOST"
else
  echo ">> certbot failed (DNS may still be propagating). HTTP works; retry later with:"
  echo "   certbot --nginx -d $HOST --non-interactive --agree-tos -m $EMAIL --redirect"
fi

echo "=== DONE ==="
curl -s -o /dev/null -w ">> local app health: HTTP %{http_code}\n" http://127.0.0.1:8700/health || true
