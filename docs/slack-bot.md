<!-- =========================================================================
slack-bot.md
---------------------------------------------------------------------------
Responsible for: The operator-facing guide to the native Slack bot (C2-bots) —
                 what it does, the smallest-slice limits, the Slack-app setup, the
                 config, how to run it, and the threat model.
Role in project: The how-to companion to the scoping doc
                 (docs/archive/phase-c2-bots-kickoff.md) and the build plan. Referenced from
                 orion.toml.example and .env.example.
========================================================================= -->

# Native Slack bot (`orion bot`) — two-way in chat

> **⚠ PARKED (KI-28 Stage 2, 2026-07-07).** The bot's write target — the relay's
> `POST /api/comments` comment endpoint — was **retired** when comments were folded into the
> discussion model. Repointing the bot at the discussion write must wait for **per-user keys** (the
> Bearer discussion route stamps role `developer`, but a chat reply is *supervisor* speech — posting
> it now would be dishonest attribution). So today `orion bot` **prints a parked notice and exits**;
> the Slack shell recognizes a forwardable reply but does **not** relay it. The pure decision core
> (`orion.bot.core`) and the shell are kept as the revival seam, and `[bot]` config + the `slack-bot`
> extra still parse. The rest of this doc describes the **original C2-bots design** (a chat reply →
> `POST /api/comments` → `report_comments`); revival will **adapt** it to the discussion write (the
> comment endpoint and store no longer exist). Treat it as historical/architectural, not current
> behavior. See `docs/two-person-shared-base-kickoff.md`.

C2 made the loop two-way **on the dashboard**. The Slack bot makes it two-way **in chat**, where the
supervisor already reads the report: a reply in a mapped Slack channel becomes a message on that
project's discussion thread — visible on the dashboard *and* pulled back by `orion discussions pull`.

This is the **smallest first slice**: one platform (Slack), no slash commands, no command parsing.

## How it works (one paragraph)

The bot is an **always-on process** (`orion bot`) that holds a Slack **Socket Mode** connection —
an *outbound* WebSocket, so there is **no public inbound endpoint** to host or secure. When a
supervisor posts in a mapped channel, the bot relays it over an authenticated `POST /api/comments`
to your **relay** (the same one `orion report` already pushes to). The relay attaches it to the
project's **latest** report as a comment. The bot is just another machine client of the relay —
it does **not** replace the `orion comments` pull, and the dashboard is unchanged.

```
supervisor types in #project-updates
        │  (Socket Mode, outbound WebSocket)
        ▼
   orion bot ──POST /api/comments (Bearer)──▶ relay ──▶ report_comments
                                                          │
                          dashboard  ◀───────────────────┤
                          orion comments ◀────────────────┘
```

## What this slice does NOT do (known limits)

- **Replies attach to the project's _latest_ report.** You cannot target an older report yet — that
  needs a message→report map, a later additive change (the relay endpoint already accepts an
  optional `report_id` for it).
- **No slash commands / buttons / threads.** Structure, when it comes, will be slash commands — not
  parsed from free text.
- **Author is the Slack display name** (resolved via one `users.info` call, falling back to the user
  id). It is a self-entered-style label, **not** an authenticated identity (that is C3).

## Prerequisites

- An **enabled `[relay]`** in `orion.toml` (the bot writes into it and reuses its `url` + token).
- The optional dependency: `pip install orion[slack-bot]` (pulls in `slack-bolt`). The core install
  is unaffected for anyone not running the bot.

## Slack app setup (one-time)

Create the app at <https://api.slack.com/apps> → **Create New App** → *From scratch*.

1. **Socket Mode** → toggle **Enable Socket Mode** on. When prompted, create an **app-level token**
   with the `connections:write` scope — this is the **`xapp-…`** token (→ `ORION_SLACK_APP_TOKEN`).
2. **OAuth & Permissions** → under *Bot Token Scopes* add:
   - `channels:history` — read messages in public channels the bot is in (the replies);
   - `users:read` — resolve a user id to a readable display name.
3. **Event Subscriptions** → enable, and under *Subscribe to bot events* add **`message.channels`**
   (new messages in public channels). (Socket Mode delivers these over the WebSocket — no Request
   URL needed.)
4. **Install to Workspace** (OAuth & Permissions → *Install*). Copy the **Bot User OAuth Token**,
   the **`xoxb-…`** token (→ `ORION_SLACK_BOT_TOKEN`).
5. **Invite the bot to each channel** you'll map: in Slack, `/invite @YourBot` in that channel. The
   bot only sees channels it is a member of.
6. **Get each channel id:** Slack → Preferences → *Advanced* → enable **Developer Mode** (or just
   right-click a channel → **Copy** → **Copy channel ID**). Ids look like `C07ABC123`.

## Configure

`.env` (never committed):

```
ORION_SLACK_BOT_TOKEN=xoxb-…      # Bot User OAuth Token
ORION_SLACK_APP_TOKEN=xapp-…      # App-Level Token (connections:write)
ORION_RELAY_TOKEN=…               # the SAME relay token orion report uses
```

`orion.toml` (the bot reuses the existing `[relay]` as its write target):

```toml
[relay]
enabled       = true
url           = "https://your-relay.example/ingest"
token_env_var = "ORION_RELAY_TOKEN"

[bot]
enabled           = true
platform          = "slack"
token_env_var     = "ORION_SLACK_BOT_TOKEN"
app_token_env_var = "ORION_SLACK_APP_TOKEN"

  [[bot.channels]]
  channel_id = "C07ABC123"   # the channel to listen in
  project    = "orion"       # which project its replies attach to
```

You can list several `[[bot.channels]]` — one bot process serves them all.

## Run

```
orion bot
```

It blocks (Ctrl-C to stop), printing how many channels it is watching. Post a message in a mapped
channel, then confirm it shows up on the dashboard and via `orion comments <project>`.

**Hosting:** for the dogfood, run it locally. A separate always-on worker (e.g. alongside the Fly
relay) is a documented future, not part of this slice.

## Threat model (why this is safe)

- **No inbound public surface.** Socket Mode is an outbound WebSocket; nothing listens for inbound
  HTTP. The bot authenticates *to Slack* with the two `.env` tokens.
- **Loop prevention.** Orion delivers reports via an incoming **webhook**, which arrive flagged as
  bot/`bot_message`. The bot drops any bot/webhook-authored message, so it never relays its own
  delivered report back as a comment.
- **Configured channels only.** A message is acted on only if its channel is in `[[bot.channels]]`;
  DMs and unrelated channels are ignored.
- **Untrusted content.** Message text is treated as an opaque comment body — length-capped, **never**
  parsed as a command.
- **Authenticated, fail-soft write.** The bot→relay `POST /api/comments` is Bearer-authed (the same
  shared relay token); a failed relay call is logged and dropped, never crashing the listener.
- **Redaction stays outbound-only.** Inbound supervisor text is not secret-scanned (it's
  access-gated supervisor input, not the developer's own outbound secrets); the control on it is
  XSS-escaping on render, already in the relay.

See `docs/phase-c2-bots-kickoff.md` for the scoping decision and the
dogfood capture sheet.
