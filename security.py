from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional

import jwt
from fastapi import Query, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param

import config
from local_api_core.auth import verify_bearer_token as verify_static_bearer


_JWT_ISSUER = "maaganm-cars"
_PUBLIC_PATHS = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/favicon.ico",
}
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="JWT or configured static API token",
)


class APIException(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, repr=False)
class CarCredentials:
    account: Literal["personal", "work"]
    code: str

    def __repr__(self) -> str:
        return f"CarCredentials(account={self.account!r}, code='[REDACTED]')"


def generate_jwt_token(
    subject: str = "cars-agent", expires_days: int = 365
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=expires_days)).timestamp()),
        "iss": _JWT_ISSUER,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


async def verify_bearer_token(
    request: Request,
    _credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> Dict[str, Any]:
    """Validate a signed JWT or the configured static bearer token."""
    if request.url.path in _PUBLIC_PATHS:
        return {"sub": "public"}

    authorization = request.headers.get("Authorization")
    if not authorization:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="missing_token",
            message="Missing Authorization: Bearer <token> header",
        )

    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer" or not token:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_authorization_header",
            message="Authorization header must start with Bearer",
        )

    if verify_static_bearer(authorization, config.CARS_API_TOKEN):
        return {"sub": "static-token"}

    try:
        return jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=["HS256"],
            issuer=_JWT_ISSUER,
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="token_expired",
            message="Bearer token has expired",
        )
    except jwt.InvalidTokenError:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_token",
            message="Invalid Bearer token",
        )


async def get_car_credentials(
    request: Request,
    account: Literal["personal", "work"] = Query("personal"),
) -> CarCredentials:
    """Resolve the selected account's locally configured Cars code."""
    del request
    code = (
        config.CARS_PERSONAL_CODE if account == "personal" else config.CARS_WORK_CODE
    ).strip()
    if not code:
        raise APIException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="credentials_missing",
            message=f"Cars credentials are not configured for the {account} account.",
        )

    return CarCredentials(account=account, code=code)
