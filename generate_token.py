"""Generate and securely save a local JWT bearer token."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


_TOKEN_PATH = Path(__file__).resolve().parent / ".current_jwt"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a JWT Bearer token for maaganm-cars"
    )
    parser.add_argument(
        "--subject", default="cars-agent", help="Subject identifier for the token"
    )
    parser.add_argument(
        "--days", type=int, default=365, help="Token validity in days (must be positive)"
    )
    args = parser.parse_args()

    if args.days <= 0:
        parser.error("--days must be a positive integer")

    # Import only when executing the command so importing this module stays lightweight.
    from security import generate_jwt_token

    token = generate_jwt_token(subject=args.subject, expires_days=args.days)
    fd = os.open(_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            fd = -1
            token_file.write(token.strip())
    finally:
        if fd != -1:
            os.close(fd)
    print(_TOKEN_PATH)


if __name__ == "__main__":
    main()
