# AGENTS.md — gcalbusysync

Conventions, patterns, and hard-won API discoveries for AI agents working on this codebase.

---

## Documentation updates

When making any code change, **automatically** update the following without being asked:
- **`AGENTS.md`** — keep API quirks, conventions, and algorithm descriptions current
- **`README.md`** — keep user-facing docs, config reference, and examples current
- **`config.sample.yaml`** — keep comments accurate when config structure changes
- **Python source files** — keep docstrings and inline comments current with the code they describe
- **`com.gcalbusysync.plist`** — keep CLI arguments and paths current if they change

After any change that affects CLI arguments, paths, or scheduling behaviour, **always tell the user** they need to update their installed LaunchAgent and/or crontab:
```
Remember to copy the updated plist and reload the LaunchAgent:
  cp "com.gcalbusysync.plist" ~/Library/LaunchAgents/
  launchctl unload ~/Library/LaunchAgents/com.gcalbusysync.plist
  launchctl load   ~/Library/LaunchAgents/com.gcalbusysync.plist
Or update your crontab manually if you use cron instead.
```

---

## Project purpose

gcalbusysync syncs busy/free status across multiple Google Calendar accounts in a many-to-many mesh. For each account it reads events from the source calendar and creates native **Out of Office** (`outOfOffice`) blocks on all other accounts' target calendars — so each calendar reflects unavailability from all others, without leaking event details. It runs as a scheduled cron job or macOS LaunchAgent.

---

## Directory layout

**Repo (tracked by git) — source code only:**
```
gcalbusysync/
├── gcalsync/
│   ├── auth.py             # OAuth2 credential management
│   ├── calendar_client.py  # Google Calendar API wrapper (most complex module)
│   ├── config.py           # Config loading and validation
│   ├── models.py           # Dataclasses: OooConfig, AccountConfig, SyncConfig, AppConfig
│   ├── state.py            # Sync token persistence
│   └── sync.py             # Core sync algorithm
├── config.sample.yaml      # Sample config copied to ~/.gcalbusysync/ on first run
├── main.py                 # CLI entry point (auth / sync / cleanup subcommands)
├── requirements.txt
└── com.gcalbusysync.plist  # macOS LaunchAgent template
```

**Runtime (never in git), auto-created at `~/.gcalbusysync/` on first run:**
```
~/.gcalbusysync/            # mode 0700
├── config.yaml             # live config (copied from config.sample.yaml on first run)
├── credentials/            # mode 0700
│   ├── <safe_id>_credentials.json  # OAuth client secret from Google Cloud Console
│   └── <safe_id>_token.json        # OAuth token (auto-created by `auth` command)
├── sync_state.json         # persisted nextSyncTokens per account
└── logs/
    └── gcalsync.log        # rotating log
```

`<safe_id>` is the account `id` with all non-`[a-zA-Z0-9_]` characters replaced by `_`.
Example: `you@gmail.com` → `you_gmail_com`. This transformation is done by `_safe_id()` in `config.py`.

---

## Critical Google Calendar API quirks

These are non-obvious behaviours that caused real bugs and debugging sessions. Read this section before touching any `events.list` call.

### 1. `outOfOffice` events are excluded from `events.list` by default

`events.list` only returns `default` event types unless explicitly told otherwise. Without the `eventTypes` parameter, OOO events are completely invisible — no error, no hint, just silently missing results.

**Always** include `eventTypes=["outOfOffice"]` when listing or querying OOO events:

```python
params = dict(
    calendarId=...,
    timeMin=...,
    timeMax=...,
    singleEvents=True,
    eventTypes=["outOfOffice"],   # required — omitting this returns zero OOO events
    pageToken=page_token,
)
```

### 2. `privateExtendedProperty` server-side filter does NOT work on `outOfOffice` events

This is the single most painful quirk in the codebase. The Google Calendar API supports a `privateExtendedProperty=key=value` filter parameter on `events.list`, but **this filter is silently broken for `outOfOffice` event types** — it always returns zero results regardless of the actual property values stored on the events. No error is raised.

**Do NOT use `privateExtendedProperty` filter parameters for `outOfOffice` event queries.**

The correct pattern (used throughout the codebase) is:
1. Fetch all `outOfOffice` events in a time window using `eventTypes=["outOfOffice"]`
2. Filter **client-side** by inspecting `extendedProperties.private` in the returned event dicts

Both `find_busy_block_for_event()` and `list_all_busy_blocks()` in `calendar_client.py` use this approach.

### 3. `outOfOffice` events require `status: "confirmed"`

The API rejects `outOfOffice` events with other status values. Always use `"status": "confirmed"` in the event body.

### 4. `transparency` field is ignored on `outOfOffice` events

Do not set `transparency` on `outOfOffice` events — the field is silently ignored. OOO events are always treated as busy by the API regardless of this field.

### 5. `privateExtendedProperties` ARE persisted on `outOfOffice` events

Despite the filter not working (quirk #2 above), the properties themselves **are** stored correctly and returned in `events.list` results. Client-side filtering on the returned dicts works correctly — it's only the server-side `?privateExtendedProperty=` query parameter that fails.

---

## Event tracking convention

Every gcalsync-created OOO block stores two private extended properties (invisible in the Google Calendar UI):

| Property key | Value |
|---|---|
| `gcalsync_source_event_id` | The source event's Google Calendar ID |
| `gcalsync_source_account` | The source account's `id` from config |

Constants defined in `calendar_client.py`:
```python
PROP_SOURCE_EVENT_ID = "gcalsync_source_event_id"
PROP_SOURCE_ACCOUNT  = "gcalsync_source_account"
```

`_is_busy_block(event: dict) -> bool` in `calendar_client.py` returns `True` if both keys are present in `extendedProperties.private`. This function is imported into `sync.py` and used inside `is_busy_source()` to skip gcalsync-created OOO blocks when reading source events — without this check, the tool would re-sync its own OOO blocks to other calendars and multiply them on every run.

---

## Sync algorithm overview

`run_sync()` in `sync.py`:
1. Build a `CalendarClient` for every account (validates auth upfront — fails fast before touching any calendars)
2. Load `~/.gcalbusysync/sync_state.json`
3. For each source account → `_sync_source_account()`:
   - Try **incremental sync** using stored `nextSyncToken` — returns only changed events since the last run
   - HTTP 410 (stale/expired token) → fall back to **full sync** (time-bounded by `days_behind`/`days_ahead`)
   - For each changed event → `_process_event()` fans out to all other accounts
   - On full sync only: run `_reconcile_orphaned_blocks()` to delete OOO blocks whose source event is no longer present (the full sync API omits cancelled events, so reconciliation is required)
   - Store new `nextSyncToken` only after successful processing — failed runs retry on the next cycle
4. Save state

`_process_event()` logic per target account:
- `is_busy_source(event)` → should this event have an OOO block?
  - `False` if: the event is a gcalsync OOO block itself (prevents loops), cancelled, or transparent (marked "free")
- Find existing OOO block via `find_busy_block_for_event()` (time-window fetch + client-side filter — see quirks above)
- If should sync + block exists → update times if changed, otherwise log "up to date"
- If should sync + no block → `create_ooo_block()`
- If should not sync + block exists → `delete_busy_block()`

---

## OOO auto-decline config

Google Calendar's `outOfOffice` event type supports automatic meeting decline via `outOfOfficeProperties`. The config controls this at two levels with clear precedence:

```yaml
sync:
  ooo:                    # global default (optional)
    auto_decline: "all"   # "none" | "all" | "new"
    decline_message: "Out of office."

accounts:
  - id: "you@work.com"
    ooo:                  # per-account override (takes precedence over global)
      auto_decline: "none"
  - id: "you@gmail.com"
    # no ooo key → inherits global default
```

**Precedence**: per-account `ooo` → global `sync.ooo` → omit `outOfOfficeProperties` entirely (Google uses the account owner's personal Calendar setting).

If the global `ooo` section is absent, `outOfOfficeProperties` is not sent in the API call at all.

API value mapping (in `_AUTO_DECLINE_MAP` in `calendar_client.py`):

| Config `auto_decline` | API `autoDeclineMode` |
|---|---|
| `"none"` | `"declineNone"` |
| `"all"` | `"declineAllConflictingInvitations"` |
| `"new"` | `"declineOnlyNewConflictingInvitations"` |

---

## Key implementation notes

- **`singleEvents=True`** on all `events.list` calls — expands recurring events into individual instances so each instance can be tracked and cleaned up independently.
- **Reminders suppressed** on created OOO blocks: `"reminders": {"useDefault": False, "overrides": []}` in the event body — without this, the target calendar's default reminders are inherited, causing unwanted notifications for the account owner.
- **Account `id` is immutable after first sync** — it's stored in every OOO block's `extendedProperties`. Changing an `id` orphans all existing blocks (they can never be found or cleaned up via the tracking properties). Use `python main.py cleanup` before changing any account IDs.
- **`_safe_id()`** in `config.py` converts account IDs to filesystem-safe filename stems by replacing all non-`[a-zA-Z0-9_]` characters with `_`.
- **Error isolation**: `_process_event()` wraps each target account in `try/except` so one account failure doesn't abort processing of others. The same pattern is used in `run_sync()` per source account.
- **404 on delete is silently ignored**: `delete_busy_block()` swallows HTTP 404 — the block being already gone is a normal race condition, not an error.
- **0-minute events are skipped**: Events where `start == end` (used as reminders in Google Calendar) are excluded by `is_busy_source()` and never synced as OOO blocks.

---

## Common operations

```bash
# First-time setup — interactive OAuth2 browser flow for all configured accounts
python main.py auth

# Normal operation — called by cron / LaunchAgent every 15 minutes
python main.py sync

# Full reset — wipes all gcalsync OOO blocks from all calendars, clears sync tokens
# Use this when something went wrong or before changing account IDs
python main.py cleanup

# Increase log verbosity for debugging
# Set logging.level: "DEBUG" in ~/.gcalbusysync/config.yaml
```

---

## Adding a new account

1. Create OAuth2 credentials in Google Cloud Console (application type: Desktop app)
2. Download the JSON and place it at `~/.gcalbusysync/credentials/<safe_id>_credentials.json`
3. Add the account to `~/.gcalbusysync/config.yaml`:
   ```yaml
   accounts:
     - id: "newaccount@example.com"   # only required field
   ```
4. Run `python main.py auth` — authenticates only accounts that lack a valid token file
5. Run `python main.py sync` — performs a full sync for the new account and creates OOO blocks

---

## Testing a sync change

Standard verification sequence after any change to sync logic:

1. `python main.py cleanup` — wipe all OOO blocks and reset sync state to a known-clean baseline
2. `python main.py sync` — creates fresh OOO blocks; check log for "Created OOO block" entries
3. `python main.py sync` again — should show "OOO block ... is up to date" for every event, **zero** new blocks created (confirms no duplication)
4. Change a source event to "free" in Google Calendar (Show as → Free), run sync → the corresponding OOO block should be deleted from all target calendars
5. Delete a source event, run sync → OOO block should be deleted (exercises the reconciliation path via `_reconcile_orphaned_blocks()`)
