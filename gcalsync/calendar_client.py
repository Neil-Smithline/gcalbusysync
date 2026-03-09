import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from googleapiclient.errors import HttpError

from gcalsync.models import AccountConfig, OooConfig

logger = logging.getLogger(__name__)

# Keys used in privateExtendedProperties on synced OOO blocks
PROP_SOURCE_EVENT_ID = "gcalsync_source_event_id"
PROP_SOURCE_ACCOUNT = "gcalsync_source_account"

# Mapping from config-friendly short values to the Google Calendar API enum strings
_AUTO_DECLINE_MAP = {
    "none": "declineNone",
    "all": "declineAllConflictingInvitations",
    "new": "declineOnlyNewConflictingInvitations",
}


def _is_busy_block(event: dict) -> bool:
    """Return True if this event is a gcalsync-created OOO block."""
    private = event.get("extendedProperties", {}).get("private", {})
    return PROP_SOURCE_EVENT_ID in private and PROP_SOURCE_ACCOUNT in private


def _event_time_to_datetime(time_dict: dict) -> datetime:
    """
    Extract a UTC-aware datetime from a Google Calendar event start/end dict.

    Google Calendar represents times as either:
      {"dateTime": "2024-01-15T09:00:00-05:00"}  — timed events
      {"date": "2024-01-15"}                      — all-day events
    """
    if "dateTime" in time_dict:
        return datetime.fromisoformat(time_dict["dateTime"]).astimezone(timezone.utc)
    # All-day event: treat the date as midnight UTC
    return datetime.fromisoformat(time_dict["date"]).replace(tzinfo=timezone.utc)


def _build_ooo_properties(ooo: Optional[OooConfig]) -> Optional[dict]:
    """
    Build the outOfOfficeProperties dict for the Google Calendar API, or return
    None to omit the field entirely (Google will use the account's personal setting).

    Args:
        ooo: OooConfig with auto_decline and optional decline_message, or None.

    Returns:
        A dict suitable for the 'outOfOfficeProperties' API field, or None.
    """
    if ooo is None or ooo.auto_decline is None:
        return None
    mode = _AUTO_DECLINE_MAP.get(ooo.auto_decline)
    if mode is None:
        # Should have been caught at config-load time by _parse_ooo(), but guard here too
        raise ValueError(
            f"Invalid auto_decline value: {ooo.auto_decline!r}. "
            f"Must be one of: {', '.join(sorted(_AUTO_DECLINE_MAP))}"
        )
    props: dict = {"autoDeclineMode": mode}
    if ooo.decline_message:
        props["declineMessage"] = ooo.decline_message
    return props


class CalendarClient:
    """Thin wrapper around the Google Calendar API v3 service."""

    def __init__(self, service, account: AccountConfig):
        self.service = service
        self.account = account
        self.source_calendar_id = account.source_calendar
        self.target_calendar_id = account.target_calendar

    # ------------------------------------------------------------------ #
    # Reading source events
    # ------------------------------------------------------------------ #

    def list_events_full(
        self, time_min: datetime, time_max: datetime
    ) -> tuple[list[dict], str]:
        """
        Full sync: fetch all events in the given time window.
        Handles pagination internally.

        Returns:
            (events_list, nextSyncToken)
        """
        all_events = []
        page_token = None
        next_sync_token = None

        while True:
            params = dict(
                calendarId=self.source_calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,  # expand recurring events into individual instances
                # orderBy is intentionally omitted: Google Calendar omits nextSyncToken
                # from the response when orderBy is set, breaking incremental sync.
                pageToken=page_token,
            )
            resp = self.service.events().list(**params).execute()
            all_events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            next_sync_token = resp.get("nextSyncToken", next_sync_token)
            if not page_token:
                break

        if next_sync_token is None:
            logger.warning(
                "Full sync for %s: API did not return a nextSyncToken — "
                "incremental sync will not be possible; next run will be a full sync",
                self.account.id,
            )
        logger.debug(
            "Full sync for %s: fetched %d events", self.account.id, len(all_events)
        )
        return all_events, next_sync_token

    def list_events_incremental(self, sync_token: str) -> tuple[list[dict], str]:
        """
        Incremental sync using a stored nextSyncToken.
        Only returns events that changed since the token was issued.
        Handles pagination internally.

        Raises:
            HttpError: with status 410 if the token is stale (caller must fall
                       back to full sync).

        Returns:
            (changed_events, nextSyncToken)
        """
        all_events = []
        page_token = None
        next_sync_token = None

        while True:
            params = dict(
                calendarId=self.source_calendar_id,
                syncToken=sync_token,
                singleEvents=True,
                pageToken=page_token,
            )
            resp = self.service.events().list(**params).execute()
            all_events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            next_sync_token = resp.get("nextSyncToken", next_sync_token)
            if not page_token:
                break

        logger.debug(
            "Incremental sync for %s: %d changed events", self.account.id, len(all_events)
        )
        return all_events, next_sync_token

    # ------------------------------------------------------------------ #
    # Finding existing OOO blocks on the target calendar
    # ------------------------------------------------------------------ #

    def find_busy_block_for_event(
        self,
        source_event_id: str,
        source_account_id: str,
        source_event_start: dict,
        source_event_end: dict,
    ) -> Optional[dict]:
        """
        Find the OOO block on this account's target calendar that corresponds
        to a specific source event.

        Uses a time-windowed fetch + client-side filter. The Google Calendar API's
        privateExtendedProperty server-side filter does not work on outOfOffice events
        (properties appear to be silently dropped or un-indexed for this event type),
        so we fetch all outOfOffice events in a narrow window around the source event
        and filter client-side by the tracking extended properties.

        Args:
            source_event_id:    The ID of the source event.
            source_account_id:  The account ID the source event belongs to.
            source_event_start: The source event's start dict ({"dateTime": ...} or {"date": ...}).
            source_event_end:   The source event's end dict.

        Returns:
            The matching OOO block event dict, or None.
        """
        # Build a window ±1 day around the source event to catch the corresponding block
        time_min = _event_time_to_datetime(source_event_start) - timedelta(days=1)
        time_max = _event_time_to_datetime(source_event_end) + timedelta(days=1)

        all_events = []
        page_token = None
        while True:
            params = dict(
                calendarId=self.target_calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                eventTypes=["outOfOffice"],
                pageToken=page_token,
            )
            resp = self.service.events().list(**params).execute()
            all_events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        matches = [
            e for e in all_events
            if e.get("extendedProperties", {}).get("private", {}).get(PROP_SOURCE_EVENT_ID) == source_event_id
            and e.get("extendedProperties", {}).get("private", {}).get(PROP_SOURCE_ACCOUNT) == source_account_id
        ]
        if len(matches) > 1:
            logger.warning(
                "Found %d OOO blocks for source event %s/%s on account %s - expected 1",
                len(matches), source_account_id, source_event_id, self.account.id,
            )
        return matches[0] if matches else None

    def list_all_busy_blocks(
        self, time_min: datetime, time_max: datetime
    ) -> list[dict]:
        """
        Return all synced OOO blocks on this account's target calendar within
        the given time window. Fetches all events and filters client-side by
        the presence of both gcalsync private extended property keys.

        Used by the cleanup command to find every block regardless of which
        source account created it.
        """
        all_events = []
        page_token = None

        while True:
            params = dict(
                calendarId=self.target_calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                eventTypes=["outOfOffice"],  # outOfOffice events are omitted by default
                pageToken=page_token,
            )
            resp = self.service.events().list(**params).execute()
            all_events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        busy_blocks = [e for e in all_events if _is_busy_block(e)]
        logger.debug(
            "Account %s: found %d OOO block(s) out of %d total events",
            self.account.id, len(busy_blocks), len(all_events),
        )
        return busy_blocks

    # ------------------------------------------------------------------ #
    # Writing OOO blocks to the target calendar
    # ------------------------------------------------------------------ #

    def create_ooo_block(
        self,
        source_event: dict,
        source_account_id: str,
        ooo: Optional[OooConfig] = None,
    ) -> dict:
        """
        Create an Out of Office event on this account's target calendar,
        mirroring the source event's time window. Uses Google Calendar's
        native outOfOffice eventType so it renders correctly in the UI.
        Stores tracking properties so the block can be found and deleted
        if the source event changes.

        Args:
            source_event:      The source calendar event dict.
            source_account_id: The id of the account the source event belongs to.
            ooo:               OooConfig controlling auto-decline behaviour.
                               None → outOfOfficeProperties is omitted (Google uses
                               the account owner's personal default setting).
        """
        body = {
            "summary": "OOO",
            "eventType": "outOfOffice",
            "status": "confirmed",
            "start": source_event["start"],
            "end": source_event["end"],
            "reminders": {"useDefault": False, "overrides": []},
            "extendedProperties": {
                "private": {
                    PROP_SOURCE_EVENT_ID: source_event["id"],
                    PROP_SOURCE_ACCOUNT: source_account_id,
                }
            },
        }
        ooo_props = _build_ooo_properties(ooo)
        if ooo_props is not None:
            body["outOfOfficeProperties"] = ooo_props

        created = self.service.events().insert(
            calendarId=self.target_calendar_id, body=body
        ).execute()
        logger.info(
            "Created OOO block %s on %s (source: %s/%s, auto_decline: %s)",
            created["id"], self.account.id, source_account_id, source_event["id"],
            ooo.auto_decline if ooo else "default",
        )
        return created

    def update_busy_block_times(self, busy_block_id: str, source_event: dict) -> None:
        """Update the start/end times of an existing OOO block to match the source event."""
        body = {
            "start": source_event["start"],
            "end": source_event["end"],
        }
        self.service.events().patch(
            calendarId=self.target_calendar_id,
            eventId=busy_block_id,
            body=body,
        ).execute()
        logger.info(
            "Updated OOO block %s on %s (new times: %s - %s)",
            busy_block_id, self.account.id,
            source_event["start"], source_event["end"],
        )

    def delete_busy_block(self, event_id: str) -> None:
        """
        Delete a OOO block by its event ID on the target calendar.
        Silently ignores 404 (block already gone).
        """
        try:
            self.service.events().delete(
                calendarId=self.target_calendar_id, eventId=event_id
            ).execute()
            logger.info("Deleted OOO block %s on %s", event_id, self.account.id)
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(
                    "Busy block %s on %s already gone (404)", event_id, self.account.id
                )
            else:
                raise
