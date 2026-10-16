import os
import secrets
import tempfile
from pathlib import Path

from dotenv import load_dotenv


_PROJECT_DIR = Path(__file__).resolve().parent

# Local overrides take precedence over the shared project environment file, while
# values supplied by the process environment always win.
load_dotenv(_PROJECT_DIR / ".env.local", override=False)
load_dotenv(_PROJECT_DIR / ".env", override=False)


def _read_jwt_secret(path: Path) -> str:
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise RuntimeError(f"JWT secret file is empty: {path}")
    return secret


def _load_or_create_jwt_secret() -> str:
    configured_secret = os.getenv("JWT_SECRET", "").strip()
    if configured_secret:
        return configured_secret

    secret_path = _PROJECT_DIR / ".jwt_secret"
    try:
        return _read_jwt_secret(secret_path)
    except FileNotFoundError:
        pass

    # Prepare the complete, durable file before publishing it. Hard-linking the
    # candidate into place is atomic and fails if another worker won the race.
    secret = secrets.token_hex(32)
    descriptor, candidate_name = tempfile.mkstemp(
        prefix=".jwt_secret.", dir=_PROJECT_DIR
    )
    candidate_path = Path(candidate_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as candidate:
            descriptor = -1
            candidate.write(secret + "\n")
            candidate.flush()
            os.fsync(candidate.fileno())

        try:
            os.link(candidate_path, secret_path)
        except FileExistsError:
            return _read_jwt_secret(secret_path)
        return secret
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        candidate_path.unlink(missing_ok=True)


CARS_BASE_URL = os.getenv(
    "CARS_BASE_URL", "https://sidur.maaganm.co.il"
).rstrip("/")
CARS_API_TOKEN = os.getenv("CARS_API_TOKEN", "") or (
    (_PROJECT_DIR / ".api_token").read_text(encoding="utf-8").strip()
    if (_PROJECT_DIR / ".api_token").exists() else ""
)
CARS_PERSONAL_CODE = os.getenv("CARS_PERSONAL_CODE", "")
CARS_WORK_CODE = os.getenv("CARS_WORK_CODE", "")

JWT_SECRET = _load_or_create_jwt_secret()
JWT_ALGORITHM = "HS256"

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8002"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30.0"))

# Mail-bus identities have no built-in defaults; set them in .env.local.
GMAIL_COMMAND_ALIAS = os.getenv("GMAIL_COMMAND_ALIAS", "").strip()
GMAIL_ALLOWED_SENDER = os.getenv("GMAIL_ALLOWED_SENDER", "").strip()
GMAIL_RESULT_RECIPIENT = os.getenv("GMAIL_RESULT_RECIPIENT", "").strip()
GMAIL_HMAC_SECRET = os.getenv("GMAIL_HMAC_SECRET", "").strip()
GMAIL_STATE_DB_PATH = Path(
    os.getenv("GMAIL_STATE_DB_PATH", "").strip()
    or str(_PROJECT_DIR / ".command_bus.sqlite3")
)
GMAIL_PROCESSED_LABEL = (
    os.getenv("GMAIL_PROCESSED_LABEL", "").strip() or "Agents"
)
GMAIL_POLL_INTERVAL = float(
    os.getenv("GMAIL_POLL_INTERVAL", "").strip() or "30.0"
)
GAPI_EXECUTABLE = os.getenv("GAPI_EXECUTABLE", "").strip() or "gapi"
GAPI_TIMEOUT = float(os.getenv("GAPI_TIMEOUT", "").strip() or "30.0")
GMAIL_CREDENTIALS_FILE = Path(
    os.getenv("GMAIL_CREDENTIALS_FILE", "").strip()
    or "~/.hermes/google_token.json"
)
