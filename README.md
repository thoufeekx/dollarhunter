# Dollar Hunter

**Your grocery list, cheaper, by text.** Customers text a phone number with what they need — a typed list, a
receipt photo, a PDF or a voice note. An AI agent keeps their basket, searches this week's flyers at every
grocery store near their postal code, and texts back a quote: the cheapest sensible trip (one to three stores),
what each item costs where, the savings, and a direct flyer link for each store.

Live prototype: <https://dh.thoufeekxlabs.com>

```
you>  K2C 0C1
dh>   Got you, Ottawa (Carlington). Nearby: Walmart on Baseline, Loblaws on Merivale, Food Basics. What do you need?
you>  yo get me tolet papr, 2 dozn egs n keychup, i always buy heinz. wats the total
dh>   Best trip: 2 stores. Total $16.65
      No Frills: 2 eggs (No Name) $6.66, ketchup (Heinz) $4.99 flipp.com/en-ca/flyer/8191873
      Loblaws: toilet paper (Royale Bathroom) $5.00 flipp.com/en-ca/flyer/8191917
      All at IGA: $40.96
      Prices before tax. Reply LINKS for each item's own link.
```

See [docs/architecture.pdf](docs/architecture.pdf) for the system diagrams.

## How it works

| Step | What does it |
| --- | --- |
| SMS / MMS in and out | Twilio webhook (signature checked), durable inbox, job queue, outbox |
| Understanding the customer | LLM agent with tools (Claude Haiku 5.5 via OpenRouter, backup GPT-6 Luna) |
| Memory per phone number | Postgres: location, saved preferences, basket, previous list, quotes, recent messages |
| Where the customer is | Bundled GeoNames postal-area data (any Canadian postal code, even just "K2C") |
| Prices | Every grocery and pharmacy flyer for the area, downloaded in bulk (about 9,000 items for an Ottawa area) twice a day; regular online prices and store websites (Firecrawl) as fallbacks |
| Which product is it? | Jev decision model (`~typesafe/jev-latest`): one request matches every basket item at every store |
| Store trip and totals | Plain code: tries every 1–3 store combination, weighs price against extra stops |
| Receipts, PDFs, voice notes | Gemini Flash-Lite reads photos and scanned PDFs; text PDFs are read locally; Voxtral transcribes voice |
| Files | Cloudflare R2, private, deleted after 7 days |

The model never invents a price: every number in a quote comes from stored flyer data, and a reply in which the
model writes its own prices is sent back. STOP, HELP and START are handled in code before any model sees the
message.

## Run it locally

Requires Docker.

```bash
cp .env.example .env          # set DH_ADMIN_TOKEN; add API keys to go beyond the free mock modes
make up && make migrate && make seed
open http://localhost:8000    # landing page; /demo is an admin-only chat that never sends real texts
```

Without API keys everything still runs: SMS goes to the console, AI features use clearly labelled mocks and
prices come from a small fixture catalogue.

| Setting | What it enables |
| --- | --- |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `DH_SMS_PROVIDER=twilio` | real texting |
| `DH_AI_PROVIDER=openrouter`, `DH_AI_API_KEY`, model settings | the agent, receipt reading, voice, Jev matching |
| `FIRECRAWL_API_KEY` | store-website prices for items no flyer covers |
| `DH_STORAGE_BACKEND=r2`, `R2_*` | private file storage in Cloudflare R2 |

## Tests

```bash
make test                          # 50+ unit and integration tests, no network or API keys needed
python3 scripts/scenarios.py       # plays 14 real conversations against the running app and prints every reply
python3 scripts/scenarios.py --server   # the same against the deployed server (fake numbers, no real texts)
```

Scenarios cover onboarding with partial or invalid postal codes, misspellings, corrections, clearing the list
while keeping memory, options and picks, trip modes, budgets, off-topic and prompt-injection messages, STOP,
French, brand-new areas and long conversations.

## Deploy

[deploy/README.md](deploy/README.md) runs it on a shared server with hard CPU and memory caps, no published ports
(traffic arrives through its own Cloudflare tunnel), daily self-maintenance, nightly backups and a one-command
uninstall. Server-specific settings live only in the untracked `.env.server`.

## Project layout

```
app/
  agent.py           the agent: prompt, tools, guards (no model-written prices, no claimed-but-not-done changes)
  matcher.py         Jev product matching (one call per basket), chat-model fallback
  comparison.py      store-trip planning
  providers/         catalog (bulk flyers), flipp (live search), webprices (Firecrawl), places, ai, sms
  services/          inbound, receipts, quotes, basket, outbox, jobs, retention
  routers/           Twilio webhook, health, landing, admin demo
templates/           landing page and admin demo chat
deploy/              server compose file, maintenance timer, uninstall
scripts/             deploy, server env, quick tunnel, scenario runner
tests/
```

## Caveats

- **Flyer data has no official API.** Prices come from the public endpoints the Flipp app itself uses; they can
  change or be rate limited, and a commercial launch needs a data agreement. Store-website prices are the
  store's default branch, not necessarily the customer's.
- **Quotes only.** It does not place orders or take payment.
- Postal areas © [GeoNames](https://www.geonames.org) (CC BY 4.0); store locations © OpenStreetMap contributors.
