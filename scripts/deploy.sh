#!/usr/bin/env bash
# Deploy (or update) Dollar Hunter on a server that may also run other apps, isolated from all of them.
#   scripts/deploy.sh            install or update
# Needs in .env.server (never committed): DH_DEPLOY_HOST (ssh host), DH_TUNNEL_ID (Cloudflare tunnel id, its
# credentials JSON in ~/.cloudflared) and optionally DH_PROTECTED_FILES (other apps' config files to watch).
# Safety: snapshots every other container and the protected files before and after; fails loudly on any change.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[ -f .env.server ] || { echo "missing .env.server: run scripts/make_server_env.py first" >&2; exit 1; }
setting() { grep "^$1=" .env.server | cut -d= -f2-; }
HOST=${DH_DEPLOY_HOST:-$(setting DH_DEPLOY_HOST)}
TUNNEL_ID=$(setting DH_TUNNEL_ID)
PROTECTED=$(setting DH_PROTECTED_FILES)
[ -n "$HOST" ] && [ -n "$TUNNEL_ID" ] || { echo "set DH_DEPLOY_HOST and DH_TUNNEL_ID in .env.server" >&2; exit 1; }
DIR=/opt/dollarhunter
TAG=$(date -u +%Y%m%d%H%M%S)

# Everything that is not Dollar Hunter: container ids + start times, protected files, systemd units.
snapshot() {
  ssh -o BatchMode=yes "$HOST" "
    docker ps -a --format '{{.Names}}' | grep -v dollarhunter | sort | while read -r n; do
      docker inspect -f '{{.Name}} {{.Id}} {{.State.StartedAt}} {{.State.Status}}' \"\$n\"; done
    [ -n '$PROTECTED' ] && sha256sum $PROTECTED 2>/dev/null
    ls /etc/systemd/system | grep -v dollarhunter | sort | sha256sum"
}

echo "1/6 Other apps snapshot (before)"
before=$(snapshot)

echo "2/6 Upload code to $HOST:$DIR"
ssh "$HOST" "mkdir -p $DIR/src && chmod 700 $DIR"
rsync -az --delete --exclude-from=deploy/rsync-exclude.txt ./ "$HOST:$DIR/src/"
rsync -az deploy/docker-compose.server.yml "$HOST:$DIR/docker-compose.yml"
rsync -az deploy/maintain.sh deploy/uninstall.sh "$HOST:$DIR/"
rsync -az .env.server "$HOST:$DIR/.env"
ssh "$HOST" "chmod 700 $DIR/maintain.sh $DIR/uninstall.sh && chmod 600 $DIR/.env"
ssh "$HOST" "mkdir -p $DIR/cloudflared"
sed "s/\${TUNNEL_ID}/$TUNNEL_ID/" deploy/cloudflared.yml | ssh "$HOST" "cat > $DIR/cloudflared/config.yml"
rsync -az ~/.cloudflared/"$TUNNEL_ID".json "$HOST:$DIR/cloudflared/credentials.json"
# cloudflared runs as uid 65532 inside its container
ssh "$HOST" "chown -R 65532:65532 $DIR/cloudflared && chmod 500 $DIR/cloudflared && chmod 400 $DIR/cloudflared/*"
rsync -az deploy/dollarhunter-maintenance.service deploy/dollarhunter-maintenance.timer "$HOST:/etc/systemd/system/"

echo "3/6 Build image dollarhunter-app:$TAG in the private builder"
ssh "$HOST" "set -e; cd $DIR
  docker buildx inspect dollarhunter >/dev/null 2>&1 || \
    docker buildx create --name dollarhunter --driver docker-container --driver-opt image=moby/buildkit:v0.33.1 >/dev/null
  docker buildx build --builder dollarhunter --load --label com.dollarhunter=1 -t dollarhunter-app:$TAG src
  if docker image inspect dollarhunter-app:current >/dev/null 2>&1; then docker tag dollarhunter-app:current dollarhunter-app:previous; fi
  docker tag dollarhunter-app:$TAG dollarhunter-app:current
  docker buildx stop dollarhunter  # idle builder uses memory; it restarts on the next build"

echo "4/6 Migrate database and start"
ssh "$HOST" "set -e; cd $DIR
  docker compose up -d --wait db
  docker compose run --rm --label com.dollarhunter=1 web alembic upgrade head
  docker compose run --rm --label com.dollarhunter=1 web python -m app.seed
  docker compose up -d --wait --remove-orphans
  systemctl daemon-reload && systemctl enable --now dollarhunter-maintenance.timer >/dev/null
  ./maintain.sh >/dev/null 2>&1 || true"

echo "5/6 Health"
ssh "$HOST" "cd $DIR && docker compose ps --format 'table {{.Service}}\t{{.Status}}' && \
  docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \$(docker compose ps -q)"
URL=$(grep '^DH_PUBLIC_BASE_URL=' .env.server | cut -d= -f2-)
curl -fsS -m 20 "$URL/healthz" && echo || echo "note: $URL not answering yet (DNS or certificate may still be propagating)"

echo "6/6 Other apps snapshot (after)"
after=$(snapshot)
if [ "$before" != "$after" ]; then
  echo "WARNING: something outside Dollar Hunter changed during deploy:" >&2
  diff <(echo "$before") <(echo "$after") >&2 || true
  exit 2
fi
echo "OK: other apps untouched. Dollar Hunter live at $URL"
