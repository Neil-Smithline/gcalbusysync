# gcalbusysync

Syncs busy/free status across multiple Google Calendar accounts. For each account, it reads events from its primary calendar and creates native **Out of Office** blocks on all other accounts — so each calendar reflects when you're unavailable, without exposing any event details.

Runs as a scheduled cron job or macOS LaunchAgent, syncing every 15 minutes.

## How it works

- **Many-to-many mesh**: every account blocks every other account.
- **No event details leaked**: OOO blocks contain only the title "OOO".
- **Automatic cleanup**: when a source event is deleted, changed to "free", or cancelled, the corresponding OOO block is removed from all other calendars.
- **Incremental sync**: uses Google Calendar's `nextSyncToken` to fetch only changed events on subsequent runs — fast and quota-friendly.
- **Tracking via `privateExtendedProperties`**: synced blocks store the source event ID and account as private metadata (invisible in the Google Calendar UI), so they can always be found and cleaned up.

## Setup

### 1. Google Cloud Console — create OAuth2 credentials (once per account)

For **each** Google account you want to sync:

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable the **Google Calendar API** (APIs & Services → Library → search "Google Calendar API")
4. Create credentials: APIs & Services → Credentials → Create Credentials → **OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file
5. Place the downloaded JSON file in `~/.gcalbusysync/credentials/`, named to match your account:
   - e.g. `~/.gcalbusysync/credentials/you_gmail_com_credentials.json`
   - The filename can be anything — the name is configured (or derived) in `~/.gcalbusysync/config.yaml`

> **Note**: Each Google account needs its own OAuth client ID in its own GCP project, or you can use one GCP project with multiple client IDs. The credential file is the "client secret" — it does not contain any user data and is safe to reuse across machines.

### 2. Python environment

```bash
cd "/path/to/gcalbusysync"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. First run — directory setup

On the very first run, the tool creates `~/.gcalbusysync/` and copies a sample config:

```bash
.venv/bin/python main.py sync
```

Output:
```
First run: created ~/.gcalbusysync/

Next steps:
  1. Edit ~/.gcalbusysync/config.yaml
     Add your Google account IDs under 'accounts:'

  2. Download OAuth credentials from Google Cloud Console
     and place them in ~/.gcalbusysync/credentials/
     (See README.md for detailed instructions)

  3. Run:  python main.py auth
```

### 4. Configure accounts

Edit `~/.gcalbusysync/config.yaml`. The minimum config is just a list of account IDs:

```yaml
accounts:
  - id: "you@gmail.com"
  - id: "you@yourwork.com"
```

All other fields are derived automatically from the `id`:

| Field | Default |
|---|---|
| `name` | same as `id` |
| `credentials_file` | `~/.gcalbusysync/credentials/<safe_id>_credentials.json` |
| `token_file` | `~/.gcalbusysync/credentials/<safe_id>_token.json` |
| `source_calendar` | `"primary"` |
| `target_calendar` | `"primary"` |

The `<safe_id>` is your account `id` with any non-alphanumeric characters replaced by underscores. For example, `you@gmail.com` → `you_gmail_com`, so the default credentials file is `~/.gcalbusysync/credentials/you_gmail_com_credentials.json`.

> **Important**: The `id` field is stored in every synced OOO block's private metadata. If you change an account's `id` after the first sync, orphaned OOO blocks will not be cleaned up automatically. Use `python main.py cleanup` before changing IDs.

You can override any field explicitly:

```yaml
accounts:
  - id: "you@gmail.com"
    name: "Personal"
    credentials_file: "/custom/path/credentials.json"
    token_file: "/custom/path/token.json"
    source_calendar: "primary"
    target_calendar: "primary"
```

### 5. Authenticate (once, interactive)

This opens a browser window for each account in sequence:

```bash
.venv/bin/python main.py auth
```

After granting access, token files are saved to `~/.gcalbusysync/credentials/` automatically. Subsequent runs refresh tokens silently.

### 6. Test manually

```bash
.venv/bin/python main.py sync
```

Check `~/.gcalbusysync/logs/gcalsync.log` for output and verify Out of Office blocks appear in your other calendars.

---

## Scheduling

### Option A: macOS LaunchAgent (recommended)

LaunchAgent handles sleep/wake reliably — it catches up missed intervals when the machine wakes, unlike cron which skips them.

Edit `com.gcalbusysync.plist` and update the two paths to match your environment:
- `/path/to/gcalbusysync/.venv/bin/python3` — the Python interpreter in your venv
- `/path/to/gcalbusysync/main.py` — the full path to `main.py`

The `StandardOutPath` and `StandardErrorPath` should be updated to point to `~/.gcalbusysync/logs/gcalsync.log` (or any path you prefer for LaunchAgent output).

Then install:

```bash
cp com.gcalbusysync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gcalbusysync.plist
```

Check it's running:
```bash
launchctl list | grep gcalbusysync
```

To stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.gcalbusysync.plist
```

### Option B: crontab

```bash
crontab -e
```

Add (replacing `/path/to/gcalbusysync` with your actual project directory):
```
*/15 * * * * "/path/to/gcalbusysync/.venv/bin/python3" "/path/to/gcalbusysync/main.py" sync > /dev/null
```

All logging goes to `~/.gcalbusysync/logs/gcalsync.log` automatically. The `> /dev/null` redirect suppresses stdout so cron doesn't email you normal log output on every run. stderr is left alone so cron can still email you if the job crashes at startup (e.g. missing venv, import errors) before Python logging is initialized.

---

## Commands

```bash
.venv/bin/python main.py auth      # Authenticate all accounts (run once before scheduling)
.venv/bin/python main.py sync      # Run one sync cycle
.venv/bin/python main.py cleanup   # Delete all synced OOO blocks and reset state
```

### `cleanup`

The cleanup command is a full reset: it deletes every OOO block gcalsync has ever created across all calendars, then clears the sync state so the next `sync` starts fresh.

Use it when:
- Something went wrong and you have duplicate or incorrect OOO blocks
- You want to change account IDs (run cleanup first, update config, then sync again)
- You want a clean slate for any reason

```bash
.venv/bin/python main.py cleanup
```

> **Note**: Cleanup only removes blocks within the configured `days_behind` / `days_ahead` window. To clean up blocks further in the future, temporarily increase `days_ahead` in `~/.gcalbusysync/config.yaml` before running.

---

## Configuration reference

Full config with all options (file lives at `~/.gcalbusysync/config.yaml`):

```yaml
sync:
  days_ahead: 30    # How many days ahead to sync (default: 30)
  days_behind: 1    # How many days back to look for cleanups (default: 1)

  # ooo: controls automatic meeting decline on synced OOO blocks (optional).
  # This is the global default; individual accounts can override it (see below).
  # If omitted entirely, Google uses each account owner's personal Calendar setting.
  # ooo:
  #   auto_decline: "none"    # "none" | "all" | "new" (see table below)
  #   decline_message: "I am out of the office and will respond when I return."

accounts:
  - id: "you@gmail.com"           # Only required field
    # All fields below are optional — shown here with their derived defaults:
    # name: "you@gmail.com"
    # credentials_file: "~/.gcalbusysync/credentials/you_gmail_com_credentials.json"
    # token_file: "~/.gcalbusysync/credentials/you_gmail_com_token.json"
    # source_calendar: "primary"  # Calendar to read events from
    # target_calendar: "primary"  # Calendar to write OOO blocks to
    # ooo:                        # Override global ooo for this account only
    #   auto_decline: "all"
    #   decline_message: "Unavailable — this meeting has been automatically declined."

  - id: "you@yourwork.com"

logging:
  level: "INFO"          # DEBUG, INFO, WARNING, ERROR
  # log_file defaults to: ~/.gcalbusysync/logs/gcalsync.log
  # max_bytes: 5242880   # Max log file size before rotation (5 MB)
  # backup_count: 3      # Number of rotated log files to keep
```

### OOO auto-decline values

| `auto_decline` | Behaviour |
|---|---|
| `"none"` | OOO block is created; meetings are **not** automatically declined |
| `"all"` | All conflicting meeting invitations are automatically declined |
| `"new"` | Only new (not yet responded-to) conflicting invitations are declined |
| *(omitted)* | Google uses the account owner's personal Calendar setting |

The `decline_message` is the text sent with each declined invitation. It is only used when `auto_decline` is `"all"` or `"new"`.

**Precedence**: per-account `ooo` → global `sync.ooo` → Google's personal setting.

---

## Project structure

**In the repository (tracked by git):**
```
gcalbusysync/
├── gcalsync/
│   ├── auth.py             # OAuth2 credential management
│   ├── calendar_client.py  # Google Calendar API wrapper
│   ├── config.py           # Config loading and validation
│   ├── models.py           # Data models
│   ├── state.py            # Sync token persistence
│   └── sync.py             # Core sync algorithm
├── config.sample.yaml      # Sample config (copy to ~/.gcalbusysync/ on first run)
├── main.py                 # CLI entry point
├── requirements.txt
└── com.gcalbusysync.plist  # macOS LaunchAgent template
```

**At runtime (never in git), created automatically on first run:**
```
~/.gcalbusysync/
├── config.yaml                          # Your configuration
├── credentials/
│   ├── you_gmail_com_credentials.json   # OAuth client secrets (from Google Cloud)
│   ├── you_gmail_com_token.json         # OAuth tokens (auto-created by auth command)
│   ├── you_work_com_credentials.json
│   └── you_work_com_token.json
├── sync_state.json                      # Persisted sync tokens (auto-managed)
└── logs/
    └── gcalsync.log                     # Application log
```

The `~/.gcalbusysync/` directory is created with mode `0700` (owner read/write/execute only).

---

## Troubleshooting

**"Token has been expired or revoked"** — Re-run `.venv/bin/python main.py auth` for the affected account. Delete the old token file first if needed (e.g. `~/.gcalbusysync/credentials/you_gmail_com_token.json`).

**OOO blocks not appearing** — Check `~/.gcalbusysync/logs/gcalsync.log` for errors. Ensure the source events are not marked as "free" (check the event's "Show as" field in Google Calendar — it must be "Busy", not "Free").

**OOO blocks not being cleaned up** — Automatic cleanup only runs within the `days_behind`/`days_ahead` window. Past events outside the window are not touched. For a one-time full cleanup, run `.venv/bin/python main.py cleanup` (temporarily increase `days_ahead` first if needed).

**HTTP 410 errors in the log** — Normal: it means the sync token expired (Google invalidates them after ~7 days of inactivity or when calendar settings change). The tool automatically falls back to a full sync.

**Rate limit errors** — The Google Calendar API allows 1 million requests/day. For a few accounts with typical calendar density, this is not a concern. If you have many accounts or very dense calendars, increase the sync interval.

**Duplicate OOO blocks** — Run `.venv/bin/python main.py cleanup` to wipe all gcalsync-created blocks, then run `sync` again for a clean slate.
