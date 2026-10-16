import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, Literal, Optional

import jwt
from fastapi import Header, Query, Request, Security, status
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


MEMBER_HEADER = "X-Sidur-Member"
MEMBER_FIELD = "member_id"
# Set by member_redaction.MemberQueryScrubber: query-string member codes are
# moved out of the URL before routing so they never reach access logs.
MEMBER_SCOPE_KEY = "maaganm_cars.member_ids"
LEGACY_ID_SCOPE_KEY = "maaganm_cars.legacy_id_param"
# Sidur member codes are exactly four ASCII digits (the whole upstream login).
_MEMBER_CODE_PATTERN = re.compile(r"[0-9]{4}")


@dataclass(frozen=True, repr=False)
class CarCredentials:
    account: Literal["personal", "work", "member"]
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


def _invalid_member(message: str) -> APIException:
    return APIException(
        status_code=status.HTTP_400_BAD_REQUEST,
        code="invalid_member_id",
        message=message,
    )


def validate_member_code(value: object) -> str:
    """Return a well-formed member code or raise without echoing the input."""
    if not isinstance(value, str) or not _MEMBER_CODE_PATTERN.fullmatch(value.strip()):
        raise _invalid_member("Member ID must be exactly 4 digits.")
    return value.strip()


def resolve_car_credentials(
    account: Literal["personal", "work"],
    member_values: tuple[object, ...] = (),
) -> CarCredentials:
    """Pick the caller's member code when supplied, else the account's local code."""
    if member_values:
        codes = {validate_member_code(value) for value in member_values}
        if len(codes) != 1:
            raise _invalid_member(
                "Conflicting member IDs were supplied; send exactly one."
            )
        return CarCredentials(account="member", code=codes.pop())

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


@dataclass(frozen=True, repr=False)
class CredentialRequest:
    """Credential inputs from headers and query; bodies may add a member code."""

    account: Literal["personal", "work"]
    member_values: tuple[object, ...]

    def __repr__(self) -> str:
        return (
            f"CredentialRequest(account={self.account!r}, "
            f"member_values={len(self.member_values)} redacted)"
        )

    def resolve(self, body: Optional[Any] = None) -> CarCredentials:
        """Resolve, counting a body member_id whenever it was sent, even as null."""
        values = self.member_values
        if body is not None and MEMBER_FIELD in body.model_fields_set:
            values = values + (getattr(body, MEMBER_FIELD),)
        return resolve_car_credentials(self.account, values)


AccountParam = Annotated[
    Literal["personal", "work"],
    Query(description="Locally configured account to act as when no member ID is supplied."),
]
MemberHeaderParam = Annotated[
    Optional[str],
    Header(
        alias=MEMBER_HEADER,
        description=(
            "Optional 4-digit Sidur member code: act as that kibbutz member. Takes "
            "precedence over account. Never logged or returned."
        ),
    ),
]
MemberQueryParam = Annotated[
    Optional[str],
    Query(
        alias=MEMBER_FIELD,
        description=(
            f"Compatibility alternative to the {MEMBER_HEADER} header. It is removed "
            "from the URL before routing and logging; prefer the header."
        ),
    ),
]


async def get_credential_request(
    request: Request,
    account: AccountParam = "personal",
    x_sidur_member: MemberHeaderParam = None,
    member_id: MemberQueryParam = None,
) -> CredentialRequest:
    """Collect credential inputs; endpoints with a JSON body resolve them later."""
    if request.scope.get(LEGACY_ID_SCOPE_KEY):
        raise _invalid_member(
            f"Unsupported member query parameter; send the {MEMBER_HEADER} header "
            f"or {MEMBER_FIELD}."
        )
    values: list[object] = []
    if x_sidur_member is not None:
        values.append(x_sidur_member)
    values.extend(request.scope.get(MEMBER_SCOPE_KEY, ()))
    if member_id is not None:
        values.append(member_id)
    return CredentialRequest(account=account, member_values=tuple(values))


async def get_car_credentials(
    request: Request,
    account: AccountParam = "personal",
    x_sidur_member: MemberHeaderParam = None,
    member_id: MemberQueryParam = None,
) -> CarCredentials:
    """Resolve a caller-supplied member code, else the account's local code."""
    pending = await get_credential_request(request, account, x_sidur_member, member_id)
    return pending.resolve()
