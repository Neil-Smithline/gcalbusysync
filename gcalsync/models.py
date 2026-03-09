from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OooConfig:
    """
    Controls the outOfOfficeProperties sent to Google Calendar when creating OOO blocks.

    auto_decline values:
      "none" — OOO block is created but meetings are NOT automatically declined.
      "all"  — All conflicting meeting invitations are automatically declined.
      "new"  — Only new (not yet accepted) conflicting invitations are declined.

    If auto_decline is None (default), the outOfOfficeProperties field is omitted
    entirely from the API call and Google uses the account owner's personal setting.
    """
    auto_decline: Optional[str] = None   # "none" | "all" | "new" | None (omit field)
    decline_message: Optional[str] = None  # message sent with declined invitations


@dataclass
class AccountConfig:
    id: str                           # Stable ID used in extendedProperties — never change after first run
    name: Optional[str] = None        # Human-readable label; defaults to id if not set
    credentials_file: Optional[str] = None  # Path to client_secret JSON; derived from id if not set
    token_file: Optional[str] = None        # Path to stored OAuth2 token; derived from id if not set
    source_calendar: str = "primary"  # Calendar to read events from
    target_calendar: str = "primary"  # Calendar to write OOO blocks to
    ooo: Optional[OooConfig] = None   # Per-account OOO override; None = inherit global default


@dataclass
class SyncConfig:
    days_ahead: int = 30  # How far into the future to sync
    days_behind: int = 1  # How far into the past to look (for cleanup)
    ooo: Optional[OooConfig] = None   # Global OOO default; None = let Google use account's personal setting


@dataclass
class AppConfig:
    accounts: list[AccountConfig]
    sync: SyncConfig
    logging: dict = field(default_factory=dict)
