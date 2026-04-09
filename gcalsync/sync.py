import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from googleapiclient.errors import HttpError

from gcalsync.auth import build_service
from gcalsync.calendar_client import (
    CalendarClient,
    PROP_SOURCE_ACCOUNT,
    PROP_SOURCE_EVENT_ID,
    _event_time_to_datetime,
    _is_busy_block,
)
from gcalsync.models import AccountConfig, AppConfig, OooConfig
from gcalsync.state import get_sync_token, load_state, save_state, set_sync_token

logger = logging.getLogger(__name__)


def is_busy_source(event: dict) -> bool:
    """
    Return True if an event should be synced as an OOO block.

    Criteria:
    - Must NOT be a gcalsync-created OOO block (prevents duplication loops)
    - Must have non-zero duration (start != end) — 0-minute events are used
      as reminders and should not be synced
    - status must be "confirmed" or "tentative" (not "cancelled")
    - transparency must be "opaque" (the default when not explicitly set)
      A "transparent" event means the user has marked themselves as free.
    """
    # Skip events gcalsync created - without this, OOO blocks written to a
    # target calendar would be read back as source events and re-synced to
    # other calendars, multiplying on every run.
    if _is_busy_block(event):
        return False
    # Skip 0-minute events (used as reminders — start == end, no actual duration)
    if event.get("start") == event.get("end"):
        return False
    status = event.get("status", "confirmed")
    transparency = event.get("transparency", "opaque")
    if status == "cancelled":
        return False
    if transparency == "transparent":
        return False
    return True


def _build_contained_event_ids(events: list[dict]) -> set[str]:
    """
    Return IDs of events fully contained within a strictly larger concurrent event.

    Event A is contained in event B if:
      B.start <= A.start AND A.end <= B.end AND (B.start, B.end) != (A.start, A.end)

    Only events that pass is_busy_source() are considered — transparent/cancelled
    events are neither containers nor candidates for skipping.
    """
    syncable = [
        (e, _event_time_to_datetime(e["start"]), _event_time_to_datetime(e["end"]))
        for e in events
        if is_busy_source(e)
    ]
    contained_ids: set[str] = set()
    for event, start, end in syncable:
        for other, other_start, other_end in syncable:
            if event["id"] == other["id"]:
                continue
            if other_start <= start and end <= other_end and (other_start, other_end) != (start, end):
                contained_ids.add(event["id"])
                break
    return contained_ids


def _times_differ(block: dict, source: dict) -> bool:
    """Return True if the busy block's time window differs from the source event."""
    return (
        block.get("start") != source.get("start")
        or block.get("end") != source.get("end")
    )


def _get_time_window(config: AppConfig) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    time_min = now - timedelta(days=config.sync.days_behind)
    time_max = now + timedelta(days=config.sync.days_ahead)
    return time_min, time_max


def _process_event(
    event: dict,
    source_account: AccountConfig,
    target_accounts: list[AccountConfig],
    clients: dict[str, CalendarClient],
    global_ooo: Optional[OooConfig],
    contained: bool = False,
) -> None:
    """
    For one changed source event, create, update, or delete OOO blocks
    on all target accounts as appropriate.

    The effective OOO config follows the precedence rule:
      per-account override (source_account.ooo) → global default (global_ooo) → None

    If contained=True, the event is fully inside a larger concurrent event and its
    OOO block is suppressed (any existing block is deleted).
    """
    event_id = event["id"]
    should_sync = is_busy_source(event) and not contained

    # Resolve effective OOO config: per-account override takes priority over global default
    effective_ooo = source_account.ooo if source_account.ooo is not None else global_ooo

    for target_account in target_accounts:
        try:
            target_client = clients[target_account.id]
            existing = target_client.find_busy_block_for_event(
                event_id, source_account.id, event["start"], event["end"]
            )

            if should_sync:
                if existing:
                    if _times_differ(existing, event):
                        target_client.update_busy_block_times(existing["id"], event)
                    else:
                        logger.debug(
                            "OOO block for %s/%s on %s is up to date",
                            source_account.id, event_id, target_account.id,
                        )
                else:
                    target_client.create_ooo_block(event, source_account.id, ooo=effective_ooo)
            else:
                # Event deleted, cancelled, or now free - clean up any existing OOO block
                if existing:
                    target_client.delete_busy_block(existing["id"])

        except Exception:
            logger.exception(
                "Failed to process event %s from %s on target %s",
                event_id, source_account.id, target_account.id,
            )


def _reconcile_orphaned_blocks(
    source_account: AccountConfig,
    live_event_ids: set[str],
    target_accounts: list[AccountConfig],
    clients: dict[str, CalendarClient],
    time_min: datetime,
    time_max: datetime,
) -> None:
    """
    Full-sync reconciliation pass: find OOO blocks on each target calendar
    that were created from source_account but whose source event no longer
    exists (deleted, or outside the time window). Delete them.

    This is necessary because the Google Calendar API does not return cancelled
    events in a time-bounded full sync (only in incremental sync via syncToken).
    Without this pass, deleting a source event would leave its OOO block on all
    target calendars forever whenever a full sync is triggered.
    """
    for target_account in target_accounts:
        try:
            target_client = clients[target_account.id]
            # Find all OOO blocks on this target that came from source_account
            all_blocks = target_client.list_all_busy_blocks(time_min, time_max)
            blocks_from_source = [
                b for b in all_blocks
                if b.get("extendedProperties", {}).get("private", {}).get(PROP_SOURCE_ACCOUNT) == source_account.id
            ]
            for block in blocks_from_source:
                block_source_event_id = block.get("extendedProperties", {}).get("private", {}).get(PROP_SOURCE_EVENT_ID)
                if block_source_event_id not in live_event_ids:
                    logger.info(
                        "Reconcile: deleting orphaned OOO block %s on %s "
                        "(source event %s no longer exists)",
                        block["id"], target_account.id, block_source_event_id,
                    )
                    target_client.delete_busy_block(block["id"])
        except Exception:
            logger.exception(
                "Reconciliation failed for source %s on target %s",
                source_account.id, target_account.id,
            )


def _sync_source_account(
    source_account: AccountConfig,
    all_accounts: list[AccountConfig],
    clients: dict[str, CalendarClient],
    state: dict,
    time_min: datetime,
    time_max: datetime,
    global_ooo: Optional[OooConfig],
    skip_contained_events: bool = True,
) -> None:
    """Fetch changed events from one source account and fan out to all targets."""
    source_client = clients[source_account.id]
    target_accounts = [a for a in all_accounts if a.id != source_account.id]

    sync_token = get_sync_token(state, source_account.id)
    is_full_sync = sync_token is None

    if sync_token:
        try:
            changed_events, new_sync_token = source_client.list_events_incremental(sync_token)
        except HttpError as e:
            if e.resp.status == 410:
                logger.warning(
                    "Sync token stale for account %s - falling back to full sync",
                    source_account.id,
                )
                sync_token = None
                is_full_sync = True
            else:
                raise

    if not sync_token:
        changed_events, new_sync_token = source_client.list_events_full(time_min, time_max)

    logger.info(
        "Account %s: %d changed event(s) to process",
        source_account.id, len(changed_events),
    )

    contained_ids: set[str] = set()
    if skip_contained_events:
        contained_ids = _build_contained_event_ids(changed_events)
        if contained_ids:
            logger.info(
                "Account %s: skipping %d event(s) fully contained within a larger event",
                source_account.id, len(contained_ids),
            )

    for event in changed_events:
        _process_event(
            event=event,
            source_account=source_account,
            target_accounts=target_accounts,
            clients=clients,
            global_ooo=global_ooo,
            contained=event["id"] in contained_ids,
        )

    if is_full_sync:
        # On a full sync the API omits cancelled events, so we must reconcile:
        # any OOO block whose source event isn't in the live set is an orphan.
        live_event_ids = {e["id"] for e in changed_events if e.get("status") != "cancelled"}
        _reconcile_orphaned_blocks(
            source_account=source_account,
            live_event_ids=live_event_ids,
            target_accounts=target_accounts,
            clients=clients,
            time_min=time_min,
            time_max=time_max,
        )

    # Only update the token after successful processing so we retry on next run if something failed
    set_sync_token(state, source_account.id, new_sync_token)


def run_sync(config: AppConfig) -> None:
    """
    Main entry point: run one complete sync cycle across all configured accounts.

    For each source account, reads changed events and creates/updates/deletes
    the corresponding OOO blocks on every other account.
    """
    logger.info("Starting sync cycle for %d accounts", len(config.accounts))

    # Build all API clients upfront - validates auth before touching any calendars
    clients: dict[str, CalendarClient] = {}
    for account in config.accounts:
        service = build_service(account)
        clients[account.id] = CalendarClient(service, account)

    state = load_state()
    time_min, time_max = _get_time_window(config)

    for source_account in config.accounts:
        try:
            _sync_source_account(
                source_account=source_account,
                all_accounts=config.accounts,
                clients=clients,
                state=state,
                time_min=time_min,
                time_max=time_max,
                global_ooo=config.sync.ooo,
                skip_contained_events=config.sync.skip_contained_events,
            )
        except Exception:
            logger.exception(
                "Sync failed for account %s - skipping to next account",
                source_account.id,
            )

    save_state(state)
    logger.info("Sync cycle complete.")


def run_cleanup(config: AppConfig) -> None:
    """
    Delete all synced OOO blocks from every account's target calendar,
    then clear sync_state.json so the next sync starts fresh.

    This is a hard reset — it removes every event tagged with gcalsync
    private extended properties, regardless of whether the source event
    still exists. Useful for recovering from bugs that created bad blocks.

    Note: only events within the configured time window (days_behind /
    days_ahead) are removed. Increase 'days_ahead' in config.yaml before
    running if you need a broader cleanup.
    """
    logger.info("Starting cleanup: deleting all synced OOO blocks")
    logger.info(
        "Time window: %d day(s) behind, %d day(s) ahead. "
        "Increase 'days_ahead' in config.yaml to widen the cleanup range.",
        config.sync.days_behind, config.sync.days_ahead,
    )

    # Build all clients upfront to catch auth errors before deleting anything
    clients: dict[str, CalendarClient] = {}
    for account in config.accounts:
        service = build_service(account)
        clients[account.id] = CalendarClient(service, account)

    time_min, time_max = _get_time_window(config)
    total_deleted = 0

    for account in config.accounts:
        client = clients[account.id]
        try:
            blocks = client.list_all_busy_blocks(time_min, time_max)
            deleted = 0
            for block in blocks:
                client.delete_busy_block(block["id"])
                deleted += 1
            logger.info(
                "Account %s: deleted %d of %d OOO block(s)",
                account.id, deleted, len(blocks),
            )
            total_deleted += deleted
        except Exception:
            logger.exception("Cleanup failed for account %s", account.id)

    # Reset sync state so the next run does a clean full sync
    save_state({})
    logger.info("Cleanup complete. Total OOO blocks deleted: %d. Sync state cleared.", total_deleted)
