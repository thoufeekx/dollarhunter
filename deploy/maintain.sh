#!/usr/bin/env bash
# Daily self-maintenance for Dollar Hunter (systemd timer: dollarhunter-maintenance.timer).
# Touches only Dollar Hunter: its own buildx builder, images named dollarhunter-app, and its compose project.
set -Eeuo pipefail
cd /opt/dollarhunter
exec 9>/run/lock/dollarhunter-maintenance.lock
flock -n 9 || { echo "maintenance already running"; exit 0; }
echo "[$(date -Is)] dollarhunter maintenance start"

# 1. Nightly database backup to storage; app.backup keeps the newest 7 and deletes older ones.
docker compose exec -T db pg_dump -U dollarhunter dollarhunter | gzip -9 \
  | docker compose exec -T worker python -m app.backup || echo "backup FAILED"

# 2. Build cache lives only in the private 'dollarhunter' builder: drop anything older than 3 days, cap at 300 MB.
if docker buildx inspect dollarhunter >/dev/null 2>&1; then
  docker buildx prune --builder dollarhunter --force --filter until=72h >/dev/null
  docker buildx prune --builder dollarhunter --force --keep-storage 300mb >/dev/null
fi

# 3. Keep only the :current and :previous app images; older builds are removed oldest first.
keep=$(docker image inspect -f '{{.Id}}' dollarhunter-app:current dollarhunter-app:previous 2>/dev/null | sort -u)
docker image ls dollarhunter-app --format '{{.ID}} {{.Repository}}:{{.Tag}}' | while read -r id ref; do
  full=$(docker image inspect -f '{{.Id}}' "$id")
  grep -qx "$full" <<<"$keep" || docker image rm "$ref" >/dev/null 2>&1 || true
done
docker image prune --force --filter label=com.dollarhunter=1 >/dev/null

# 4. Stopped one-off containers from migrations or backups.
docker container prune --force --filter label=com.dollarhunter=1 >/dev/null

docker system df | sed 's/^/  /'
echo "[$(date -Is)] dollarhunter maintenance done"
