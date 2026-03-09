#!/usr/bin/env python3
"""
GCal Busy Sync

Syncs busy/free status across multiple Google Calendar accounts by creating
Out of Office blocks so each account reflects unavailability from all others,
without leaking event details.

Usage:
  python main.py auth      # First-time OAuth2 setup
  python main.py sync      # Run one sync cycle
  python main.py cleanup   # Delete all synced OOO blocks and reset state

Config and credentials are stored in ~/.gcalbusysync/.
Run with no arguments to see first-run setup instructions.
"""
import argparse
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

from gcalsync.auth import get_credentials
from gcalsync.config import CONFIG_DIR, DEFAULT_CONFIG_PATH, load_config
from gcalsync.sync import run_cleanup, run_sync

# Sample config is bundled in the project directory alongside main.py
_SAMPLE_CONFIG = Path(__file__).parent / "config.sample.yaml"


def _first_run_setup() -> bool:
    """
    Detect first run and perform setup if needed.

    Returns True if setup was performed and the caller should exit,
    False if everything is already in place and execution should continue.
    """
    config_dir = CONFIG_DIR
    config_file = DEFAULT_CONFIG_PATH

    # Case 1: directory doesn't exist at all — full first-run setup
    if not config_dir.exists():
        config_dir.mkdir(mode=0o700, parents=True)
        (config_dir / "credentials").mkdir(mode=0o700)
        shutil.copy(_SAMPLE_CONFIG, config_file)
        print(f"First run: created {config_dir}/")
        print()
        print("Next steps:")
        print(f"  1. Edit {config_file}")
        print("     Add your Google account IDs under 'accounts:'")
        print()
        print("  2. Download OAuth credentials from Google Cloud Console")
        print(f"     and place them in {config_dir}/credentials/")
        print("     (See README.md for detailed instructions)")
        print()
        print("  3. Run:  python main.py auth")
        return True

    # Case 2: directory exists but config is missing — restore sample
    if not config_file.exists():
        shutil.copy(_SAMPLE_CONFIG, config_file)
        print(f"Config not found — copied sample to {config_file}")
        print(f"Edit it to add your account IDs, then run: python main.py auth")
        return True

    return False


def _setup_logging(log_config: dict) -> None:
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("log_file")
    max_bytes = log_config.get("max_bytes", 5 * 1024 * 1024)
    backup_count = log_config.get("backup_count", 3)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def cmd_auth(config) -> None:
    """Run interactive OAuth2 flow for all accounts that lack a valid token."""
    print(f"Authenticating {len(config.accounts)} account(s)...\n")
    for account in config.accounts:
        print(f"Account: {account.name} ({account.id})")
        get_credentials(account)
        print(f"  Token saved to: {account.token_file}\n")
    print("All accounts authenticated successfully.")


def cmd_sync(config) -> None:
    """Run one sync cycle."""
    run_sync(config)


def cmd_cleanup(config) -> None:
    """Delete all synced OOO blocks from all calendars and reset sync state."""
    run_cleanup(config)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GCal Busy Sync — sync busy/free status across Google Calendar accounts"
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "auth",
        help="Authenticate all accounts via OAuth2 (run once before scheduling)",
    )
    subparsers.add_parser(
        "sync",
        help="Run one sync cycle (what the cron job / LaunchAgent calls)",
    )
    subparsers.add_parser(
        "cleanup",
        help="Delete all synced OOO blocks from all calendars and reset sync state",
    )

    args = parser.parse_args()

    # First-run detection — must happen before load_config
    if _first_run_setup():
        sys.exit(0)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(config.logging)

    if args.command == "auth":
        cmd_auth(config)
    elif args.command == "sync":
        cmd_sync(config)
    elif args.command == "cleanup":
        cmd_cleanup(config)


if __name__ == "__main__":
    main()
