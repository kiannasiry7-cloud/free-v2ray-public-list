# leadmailer

Small, standalone lead finder → email writer → sender. Python 3.11, one SQLite file, one `settings.yaml`, one CLI.
First profile: `pars_default` (Pars Painting Melbourne, inner suburbs).

```
finder  →  candidate  →  writer  →  drafted  →  approve  →  approved  →  sender  →  queued  →  sent
                                   (also: rejected_duplicate, quarantined <reason>; nothing is ever deleted)
```

## Install & run

```bash
cd lead-mailer
python3 -m venv .venv && . .venv/bin/activate
pip install -e . pytest
pytest                       # 85 tests
leadmailer finder            # discover leads for every profile
leadmailer writer            # draft emails for candidates (Ollama or template)
leadmailer approve           # read each draft, y/N/q   (or: approve --all)
leadmailer sender            # dry run by default: nothing leaves the machine
leadmailer status
```

Other commands: `reject --id N --reason ...`, `release --id N --reason ...`, `unsubscribe EMAIL`, `bounce EMAIL`,
`complaint EMAIL`, `resume`. Every command takes `--settings PATH` (default `./settings.yaml` or `$LEADMAILER_SETTINGS`)
and most take `--profile NAME`.

## Real sending

1. In `settings.yaml` set `sender.dry_run: false`.
2. Run `leadmailer sender`. It asks you to type exactly `APPROVE SEND`. Anything else aborts.
   Non-interactive: `leadmailer sender --confirm "APPROVE SEND"`.

## How to change settings (`settings.yaml`)

| Want to…                              | Edit                                                                 |
|---------------------------------------|----------------------------------------------------------------------|
| Raise caps                            | `sender.hourly_cap`, `sender.daily_cap` (both **per mailbox**)       |
| Change gap between emails             | `sender.spacing_seconds`                                             |
| Change warm-up                        | `sender.warmup.steps` (daily cap for day 1, 2, 3… after first real send) |
| Add a mailbox                         | new entry under `mailboxes:`; list its name in a profile's `mailboxes:` |
| SMTP password                         | `password_source: keychain` (macOS Keychain, nothing on disk) or `env` + `password_env` |
| Use Apple Mail instead of SMTP        | mailbox `method: apple_mail` (macOS only)                            |
| Swap the AI model for a part          | `llm.use.finder|writer|sender: <provider name or none>`             |
| Add a new AI                          | new entry under `llm.providers:` with `type: ollama` or `type: openai_compatible` |
| Add a target/business                 | copy the `pars_default` block under `profiles:` with a new name, its own brief + templates folder |
| Add suburbs / keywords / sources      | the profile's `suburbs`, `keywords`, `sources` lists                  |
| Turn on scraping / crawling / verification | the optional `acquisition:` block — see **External components** |
| Change the message                    | `briefs/<profile>.yaml` (tone, offer, facts, forbidden words) and `templates/<profile>/default.*` |

Source types: `csv_file` (`company,segment,suburb,email,source_url`), `html_page` (URL with `{suburb}` / `{keyword}`
placeholders; emails are extracted from the page), and the two maps sources below. Only use public / official pages
you are allowed to contact.

Missing keys are errors, not silent defaults — except the whole `acquisition:` block, which is optional.

## External components (optional, off by default)

Three upstream projects can feed the one Finder flow. None of them is vendored, installed or required: each sits
behind an HTTP or CLI boundary, and if it is disabled, missing, or unreachable the finder logs one line and carries
on with what it already had. With no `acquisition:` block in `settings.yaml` — which is how this repo ships —
all of them are off and the finder behaves exactly as it did before they existed.

| Component | Role | Boundary |
|---|---|---|
| [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) | discover local businesses | its CSV/JSON export file, or its binary run as a subprocess |
| [unclecode/crawl4ai](https://github.com/unclecode/crawl4ai) | read each business's own public website | HTTP `POST /crawl` |
| [reacherhq/check-if-email-exists](https://github.com/reacherhq/check-if-email-exists) | no-send email verification | HTTP `POST /v0/check_email` |

### Local setup

Pin a version rather than tracking `latest`, and bind each port to loopback so nothing on the
local network can drive them:

```bash
# 1. discovery — gosom/google-maps-scraper, web runner on 8087
docker run -d --name leadmailer-gmaps --restart unless-stopped \
  -p 127.0.0.1:8087:8080 -v "$PWD/webdata:/app/webdata" \
  gosom/google-maps-scraper:v1.9.0 -web -addr :8080 -data-folder /app/webdata
#    or one-shot, writing the file a `maps_export` source reads:
#    google-maps-scraper -input queries.txt -results sources/maps.json -json

# 2. crawling — unclecode/crawl4ai, HTTP server on 11235
docker run -d --name leadmailer-crawl4ai --restart unless-stopped \
  -p 127.0.0.1:11235:11235 --shm-size=1g unclecode/crawl4ai:0.7.4

# 3. verification — reacherhq/backend, HTTP API on 8086. No mail is ever sent.
docker run -d --name leadmailer-reacher --restart unless-stopped \
  -p 127.0.0.1:8086:8080 -e RCH__HTTP_HOST=0.0.0.0 reacherhq/backend:v0.10.0
```

Readiness, without sending anything: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8087/api/v1/jobs`
returns `200`; a `GET` on Reacher's `http://127.0.0.1:8086/v0/check_email` returns `405`, because
the route exists and only accepts `POST`.

### Two limits of this arrangement, measured rather than assumed

**Reacher cannot complete an SMTP check here.** Verification needs outbound port 25, and it is
blocked on this machine — `nc -w 6 -z aspmx.l.google.com 25` fails from inside the Docker network.
Syntax and MX checks still work and are useful: an address whose domain publishes no MX comes back
`invalid` in milliseconds. An address whose domain *does* publish MX makes Reacher retry SMTP until
`acquisition.reacher.timeout_seconds` expires, which the adapter reports as `AdapterUnavailable`.
Either way the lead is recorded `unverified` or `invalid` and, because `require_verification` is
true, it is **quarantined** — never treated as a pass. To get real SMTP verification, run Reacher
somewhere with port 25 open and point `base_url` at it.

**Crawl4AI is configured but switched off here.** `unclecode/crawl4ai:0.7.4` is 2.68 GB compressed
and roughly 7 GB extracted, and this host's 20 GB Docker VM disk is already 94% full with other
projects' images. The settings block is written and pointed at `127.0.0.1:11235`, so on a host with
room the only change is `enabled: true`. Until then the finder uses the addresses each scraper row
already carried, which is exactly how it behaved before crawl4ai existed.

The `acquisition:` block below lists the built-in defaults, which apply to any key you leave out.
What this repo actually ships is stricter than the defaults on the three keys that matter —
`require_verification: true`, `min_quality_score: 45`, `reacher.enabled: true` — and
`tests/test_shipped_settings.py` fails if any of them is loosened.

```yaml
acquisition:
  max_candidates_per_run: 20        # hard ceiling on new candidates per finder run
  max_contacts_per_business: 1      # one decision-maker address per company, never a list dump
  min_quality_score: 0              # raise to make the finder pickier
  require_verification: false       # true = an address no verifier confirmed cannot become a candidate
  crawl4ai:
    enabled: false
    base_url: http://localhost:11235
    max_pages_per_site: 3
    paths: ["", contact, contact-us, about]
    api_key_env: ""                 # name of an env var, never the key itself
  reacher:
    enabled: false
    base_url: http://localhost:8080
    from_email: verify@example.com  # envelope identity for the SMTP probe; no mail is sent
    api_key_env: ""

profiles:
  pars_default:
    sources:
      - type: maps_export           # a file the scraper already produced (JSON array, JSON lines or CSV)
        path: sources/maps.json
      - type: maps_command          # or run a locally installed scraper and read its stdout
        command: [google-maps-scraper, -input, queries.txt, -json, -exit-on-inactivity, 3m]
        timeout_seconds: 600
```

### Rehearsing the whole path without contacting anybody

`settings.yaml` carries a second profile, `acquisition_smoke`, whose only source is the checked-in
sample `sources/acquisition_smoke_maps_export.json`. Every domain in that file sits under the
reserved `.test` TLD (RFC 6761), so it resolves nowhere and there is no person behind it:

```bash
leadmailer finder --profile acquisition_smoke
```

It runs the one Finder, the one ledger and the same gates as production. It touches the writer,
the approval flow and the sender not at all, so it cannot send mail even if `dry_run` were false.
Expect both admissible sample addresses to come back `invalid` / quarantined, one address per
business, and the third and fourth rows to drop out — `hello@example.com` on a junk domain and a
row with no address at all.

### What is recorded, and what is allowed to be mailed

Every stored lead — admitted or not — carries `website`, `phone`, `evidence_url` (the exact public page the address
was read from), `provenance`, `verification`, `verification_detail`, `quality_score` and `quality_reason` on its row
in the same `leads` ledger. The score is deterministic: the same inputs always produce the same number and the same
reason string (`leadmailer/quality.py`).

Only `valid` (Reacher answered *safe*) and `unverified` (no verifier ran — the project's pre-existing behaviour) may
become a `candidate`. `invalid`, `risky`, `catch_all` and `unknown` are stored as `quarantined` with the reason, and
the only way out is a human running `leadmailer release --id N --reason ...`.

### Limitations, exactly

- Discovery is **not** automated end to end: you run the scraper and this project reads its output. Nothing here
  clicks through Google Maps, and rate limits / terms of that scraper are yours to respect.
- Crawling is public pages only: the site root plus the conventional public paths listed in `paths`. No login, no
  cookies, no authenticated pages, no access-control bypass.
- Reacher needs outbound **port 25**. Most home and cloud networks block it; Reacher then answers `unknown`, which
  this project treats as not send-ready rather than as a pass. Verification is never a guarantee of deliverability.
- `max_candidates_per_run` caps candidates per finder run; the profile's `daily_cap` still caps leads observed per
  day, and the sender's own per-mailbox hourly/daily caps and warm-up are unchanged.
- Existing `csv_file` and `html_page` sources are not crawled or verified differently than before; the crawl and
  verify steps run on businesses discovered through the maps sources.
- Real sending stays off (`sender.dry_run: true` plus the `APPROVE SEND` phrase). These components only decide what
  reaches the approval queue.

### Licensing boundary

Reacher (`check-if-email-exists`) is AGPL-3.0 / commercial dual licensed. This repository contains **no** Reacher
source: `leadmailer/adapters.py` speaks to a separately deployed Reacher process over HTTP, which you run and license
yourself. crawl4ai (Apache-2.0) and google-maps-scraper are likewise separate processes you deploy. Removing the
`acquisition:` block removes the dependency entirely.

## Writer rules

Always on, regardless of the brief: no pricing, no timeline promises, no numbers that aren't in `facts`.
The brief adds `forbidden` words. A draft that breaks a rule is stored and the lead is `quarantined` with the reason.
If the LLM is unreachable and `llm.fallback_to_template: true`, the plain template is used.

## Safety gates (not in settings, cannot be turned off)

- **Suppression list**: `unsubscribe`, `bounce`, `complaint` add the address; finder quarantines it, sender skips it.
- **Duplicate-send protection**: an address that ever received a real email is never mailed again.
- **Auto-pause**: any bounce or complaint pauses all sending until you run `leadmailer resume`.
- **Single writer**: one process at a time on the database (`data/*.lock`).
- **Confirm phrase**: real sending requires `APPROVE SEND` every run.

## Layout

```
settings.yaml            all configuration
briefs/                  one brief per profile
templates/<profile>/     default.subject.txt, default.body.md  ({company} {suburb} {business} {offer} {sender_name})
sources/                 CSV sources
data/                    SQLite DB + lock (git-ignored)
leadmailer/              config, db, states, guards, llm, finder, writer, sender, cli
                         adapters (optional external components), quality (deterministic scoring)
tests/
```
