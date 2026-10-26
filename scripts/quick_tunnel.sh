#!/usr/bin/env bash
# Start a free Cloudflare quick tunnel to the local app, then point .env and the Twilio number at it.
# Quick tunnel addresses change on every start; this keeps everything in sync. Logs: data/tunnel.log
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=data/tunnel.log
mkdir -p data
pkill -f 'cloudflared tunnel --no-autoupdate --url http://localhost:8000' 2>/dev/null || true
nohup cloudflared tunnel --no-autoupdate --url http://localhost:8000 >"$LOG" 2>&1 &
URL=""
for _ in $(seq 1 30); do
  URL=$(grep -a -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$URL" ] && grep -aq 'Registered tunnel connection' "$LOG" && break
  sleep 1
done
[ -n "$URL" ] || { echo "tunnel did not start, see $LOG" >&2; exit 1; }

sed -i '' "s|^DH_PUBLIC_BASE_URL=.*|DH_PUBLIC_BASE_URL=$URL|" .env
env_val() { grep "^$1=" .env | cut -d= -f2-; }
SID=$(env_val TWILIO_ACCOUNT_SID); TOKEN=$(env_val TWILIO_AUTH_TOKEN); FROM=$(env_val TWILIO_FROM_NUMBER)
if [ -n "$SID" ] && [ -n "$FROM" ]; then
  API="https://api.twilio.com/2010-04-01/Accounts/$SID/IncomingPhoneNumbers"
  PN=$(curl -s -u "$SID:$TOKEN" "$API.json" --data-urlencode "PhoneNumber=$FROM" -G \
       | python3 -c "import sys,json;n=json.load(sys.stdin)['incoming_phone_numbers'];print(n[0]['sid'] if n else '')")
  [ -n "$PN" ] && curl -s -u "$SID:$TOKEN" -X POST "$API/$PN.json" --data-urlencode "SmsUrl=$URL/sms/inbound" \
       --data-urlencode "SmsMethod=POST" >/dev/null && echo "Twilio webhook -> $URL/sms/inbound"
fi
docker compose up -d --no-build --force-recreate --wait web worker >/dev/null
echo "App public at $URL"
