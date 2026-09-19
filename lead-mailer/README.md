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
pytest                       # 22 tests
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
| Change the message                    | `briefs/<profile>.yaml` (tone, offer, facts, forbidden words) and `templates/<profile>/default.*` |

Source types: `csv_file` (`company,segment,suburb,email,source_url`) and `html_page` (URL with `{suburb}` / `{keyword}`
placeholders; emails are extracted from the page). Only use public / official pages you are allowed to contact.

Missing keys are errors, not silent defaults.

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
tests/
```
