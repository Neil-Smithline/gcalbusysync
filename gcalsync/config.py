import re
import yaml
from pathlib import Path
from typing import Optional

from gcalsync.models import AccountConfig, AppConfig, OooConfig, SyncConfig

# Default directory for all runtime files (config, credentials, tokens, logs, state)
CONFIG_DIR = Path.home() / ".gcalbusysync"

# Default config file path
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"

# Valid auto_decline values
_VALID_AUTO_DECLINE = {"none", "all", "new"}


def _safe_id(account_id: str) -> str:
    """
    Convert an account id into a safe filename stem.
    Replaces any character that is not alphanumeric or underscore with '_'.

    Examples:
        "neilsmithline.com"  -> "neilsmithline_com"
        "me@myjob.com"       -> "me_myjob_com"
        "personal"           -> "personal"
    """
    return re.sub(r"[^a-zA-Z0-9_]", "_", account_id)


def _resolve_path(p: Optional[str], config_dir: Path) -> str:
    """
    Resolve a path string against the config directory.
    - If p is None, callers handle the missing value before calling this.
    - If p is an absolute path, return it unchanged.
    - Otherwise, resolve relative to config_dir.
    """
    path = Path(p)
    if path.is_absolute():
        return str(path)
    return str(config_dir / path)


def _parse_ooo(raw: Optional[dict]) -> Optional[OooConfig]:
    """
    Parse an 'ooo' config dict into an OooConfig, or return None if absent.

    Raises:
        ValueError: if auto_decline is present but not a valid value.
    """
    if not raw:
        return None
    auto_decline = raw.get("auto_decline")
    if auto_decline is not None and auto_decline not in _VALID_AUTO_DECLINE:
        raise ValueError(
            f"Invalid auto_decline value: {auto_decline!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_AUTO_DECLINE))}"
        )
    return OooConfig(
        auto_decline=auto_decline,
        decline_message=raw.get("decline_message"),
    )


def _fill_account_defaults(account: AccountConfig, config_dir: Path) -> AccountConfig:
    """
    Fill in any fields the user omitted, using safe_id-derived defaults.
    All derived paths are absolute, rooted at config_dir.
    """
    safe = _safe_id(account.id)
    creds_dir = config_dir / "credentials"

    if account.name is None:
        account.name = account.id

    if account.credentials_file is None:
        account.credentials_file = str(creds_dir / f"{safe}_credentials.json")
    else:
        account.credentials_file = _resolve_path(account.credentials_file, config_dir)

    if account.token_file is None:
        account.token_file = str(creds_dir / f"{safe}_token.json")
    else:
        account.token_file = _resolve_path(account.token_file, config_dir)

    return account


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """
    Load and validate the config file.

    Args:
        config_path: Path to config.yaml. Defaults to ~/.gcalbusysync/config.yaml.

    Raises:
        FileNotFoundError: if the config file does not exist.
        ValueError: if required fields are missing or constraints are violated.

    Returns:
        Fully populated AppConfig with all derived fields resolved.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw or "accounts" not in raw or not raw["accounts"]:
        raise ValueError("Config must include at least one account under 'accounts'.")

    accounts = []
    for a in raw["accounts"]:
        if "id" not in a:
            raise ValueError("Every account entry must have an 'id' field.")
        account = AccountConfig(
            id=a["id"],
            name=a.get("name"),
            credentials_file=a.get("credentials_file"),
            token_file=a.get("token_file"),
            source_calendar=a.get("source_calendar", "primary"),
            target_calendar=a.get("target_calendar", "primary"),
            ooo=_parse_ooo(a.get("ooo")),
        )
        account = _fill_account_defaults(account, CONFIG_DIR)
        accounts.append(account)

    if len(accounts) < 2:
        raise ValueError("At least 2 accounts are required for sync to have any effect.")

    ids = [a.id for a in accounts]
    if len(ids) != len(set(ids)):
        raise ValueError("Account IDs must be unique.")

    sync_raw = raw.get("sync", {})
    sync = SyncConfig(
        days_ahead=sync_raw.get("days_ahead", 30),
        days_behind=sync_raw.get("days_behind", 1),
        ooo=_parse_ooo(sync_raw.get("ooo")),
    )

    # Resolve log file path
    log_config = raw.get("logging", {})
    if "log_file" in log_config:
        log_config["log_file"] = _resolve_path(log_config["log_file"], CONFIG_DIR)
    else:
        log_config["log_file"] = str(CONFIG_DIR / "logs" / "gcalsync.log")

    return AppConfig(
        accounts=accounts,
        sync=sync,
        logging=log_config,
    )
