#!/usr/bin/env bash
# Remove Dollar Hunter from this server completely, in one go. Only things it created are touched:
# its compose project (containers, network, volumes incl. the database), its images, its private builder,
# its systemd timer and /opt/dollarhunter. Run: sudo /opt/dollarhunter/uninstall.sh   (add --yes to skip the prompt)
set -Eeuo pipefail
[ "${1:-}" = "--yes" ] || { read -r -p "This deletes Dollar Hunter and its database on this server. Type DELETE: " a; [ "$a" = DELETE ] || exit 1; }

if [ -d /opt/dollarhunter ]; then
  (cd /opt/dollarhunter && docker compose down --volumes --remove-orphans --rmi all) || true
fi
for kind in container volume network; do
  all=""; [ $kind = container ] && all="-a"
  ids=$(docker $kind ls $all -q --filter label=com.dollarhunter=1 2>/dev/null || true)
  [ -n "$ids" ] && docker $kind rm -f $ids >/dev/null 2>&1 || true
done
docker image ls dollarhunter-app -q | sort -u | xargs -r docker image rm -f >/dev/null 2>&1 || true
docker buildx rm dollarhunter >/dev/null 2>&1 || true          # removes its build cache with it
docker image rm moby/buildkit:v0.33.1 >/dev/null 2>&1 || true   # its builder image, if nothing else uses it
systemctl disable --now dollarhunter-maintenance.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/dollarhunter-maintenance.{service,timer}
systemctl daemon-reload
rm -rf /opt/dollarhunter
echo "Dollar Hunter removed from this server."
echo "Outside the server (not deleted here): the Cloudflare tunnel 'dollarhunter' and its DNS record,"
echo "the R2 bucket dollarhunter-uploads, and the Twilio number."
