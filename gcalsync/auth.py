import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from gcalsync.models import AccountConfig

logger = logging.getLogger(__name__)

# Read + write access to all calendars on the account
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_credentials(account: AccountConfig) -> Credentials:
    """
    Load stored credentials for an account, refreshing silently if expired.
    On first run (no token file), launches an interactive browser OAuth2 flow.

    Args:
        account: AccountConfig with paths to credentials and token files.

    Returns:
        Valid, refreshed google.oauth2.credentials.Credentials.
    """
    token_path = Path(account.token_file)
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Silent token refresh - no user interaction needed
            logger.debug("Refreshing expired token for account: %s", account.id)
            creds.refresh(Request())
        else:
            # First-time interactive OAuth2 flow
            logger.info("Starting first-time OAuth2 flow for account: %s", account.name)
            print(f"\n[AUTH] Opening browser for account: {account.name}")
            print(f"       Please sign in and grant calendar access when prompted.")
            flow = InstalledAppFlow.from_client_secrets_file(
                account.credentials_file, SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist token for future runs
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        logger.debug("Token saved to %s", token_path)

    return creds


def build_service(account: AccountConfig):
    """
    Build and return an authenticated Google Calendar API v3 service for an account.

    cache_discovery=False suppresses the spurious "file_cache is only supported
    with oauth2client<4.0.0" warning — file-based discovery caching is a legacy
    feature that doesn't work with the modern google-auth library this project uses.
    """
    creds = get_credentials(account)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
