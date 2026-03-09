import json
from pathlib import Path
from typing import Optional

STATE_FILE = Path.home() / ".gcalbusysync" / "sync_state.json"


def load_state() -> dict:
    """Load persisted sync tokens. Returns empty dict if file doesn't exist."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    """Persist sync tokens to disk."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_sync_token(state: dict, account_id: str) -> Optional[str]:
    """Return the stored nextSyncToken for an account, or None if not yet synced."""
    return state.get(f"sync_token_{account_id}")


def set_sync_token(state: dict, account_id: str, token: str) -> None:
    """Update the syncToken for an account in the state dict.
    Call save_state() after updating all accounts."""
    state[f"sync_token_{account_id}"] = token
