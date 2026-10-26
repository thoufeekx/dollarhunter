# Dollar Hunter on a shared server

Runs on a server that may host other apps, but shares nothing with them: own folder `/opt/dollarhunter`, own compose
project `dollarhunter` (containers, network, database volume), no published ports (traffic arrives through
its own Cloudflare tunnel), and every resource carries the label `com.dollarhunter=1`.

| Service | Memory cap | CPU cap |
| --- | --- | --- |
| db (Postgres, tuned small) | 384 MB | 0.5 |
| web | 384 MB | 0.5 |
| worker | 256 MB | 0.25 |
| tunnel | 64 MB | 0.1 |

Logs are capped at 10 MB per container (oldest file dropped first). Measured use is about 150 MB.

## Deploy or update

```bash
scripts/make_server_env.py https://dh.thoufeekxlabs.com   # first time only
scripts/deploy.sh
```

Deploy settings live only in `.env.server` (never committed): `DH_DEPLOY_HOST`, `DH_TUNNEL_ID` and optionally
`DH_PROTECTED_FILES` (space-separated config files of other apps on the host). `deploy.sh` snapshots every
non-Dollar Hunter container, those files and the systemd unit list before and after, and exits with an error if
anything outside Dollar Hunter changed.
Each build is tagged; `:current` runs and `:previous` is kept for rollback:

```bash
ssh <your-host> 'cd /opt/dollarhunter && docker tag dollarhunter-app:previous dollarhunter-app:current && docker compose up -d'
```

## Looks after itself

- Daily at 04:45 UTC (`dollarhunter-maintenance.timer` → `maintain.sh`): database backup to R2 keeping the
  newest 7, build cache pruned (older than 3 days, capped at 300 MB, in its private `dollarhunter` builder),
  app images other than `:current`/`:previous` removed, stopped one-off containers removed.
- Hourly in the worker: receipt files past 7 days deleted, finished jobs after 14 days, expired links after
  1 day, messages and events after 90 days.

## Remove completely

```bash
ssh <your-host> /opt/dollarhunter/uninstall.sh
```

Removes its containers, network, database volume, images, builder and build cache, timer and folder. The
Cloudflare tunnel and DNS record, the R2 bucket and the Twilio number live outside the server and are
removed in their own dashboards.
