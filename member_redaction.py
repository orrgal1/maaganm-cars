"""Keep caller-supplied Sidur member codes out of URLs and logs.

Uvicorn's access log formats ``scope["query_string"]`` from the same scope
object it hands to the app, so removing the member parameters here, before
routing, keeps them out of access logs, ``request.url``, and error logging.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, MutableMapping
from urllib.parse import parse_qsl, unquote_plus

from security import LEGACY_ID_SCOPE_KEY, MEMBER_FIELD, MEMBER_SCOPE_KEY

Scope = MutableMapping[str, Any]
ASGIApp = Callable[[Scope, Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]

# ``id`` is what GitHub issue #7 originally proposed. It and any case, dash, or
# bracket variant of either name (Member_ID, member_id[], member-id) are not
# honored, but they are scrubbed and rejected so a code never leaks or is
# silently ignored.
_SENSITIVE_FIELDS = {MEMBER_FIELD, "id"}


def _decoded_key(raw_pair: str) -> str:
    return unquote_plus(raw_pair.partition("=")[0])


def _normalized_key(key: str) -> str:
    return key.partition("[")[0].strip().lower().replace("-", "_")


def _is_sensitive(raw_pair: str) -> bool:
    return _normalized_key(_decoded_key(raw_pair)) in _SENSITIVE_FIELDS


def split_member_query(query_string: bytes) -> tuple[bytes, list[str], bool]:
    """Return the query without member fields, exact member_id values, and an
    unsupported-variant flag."""
    if not query_string:
        return query_string, [], False
    text = query_string.decode("latin-1")
    kept: list[str] = []
    members: list[str] = []
    legacy = False
    for raw_pair in text.split("&"):
        key = _decoded_key(raw_pair)
        if key == MEMBER_FIELD:
            # keep_blank_values: "member_id=" or a bare "member_id" is a supplied,
            # invalid value (400), not silently the default account.
            members.extend(
                value for _, value in parse_qsl(raw_pair, keep_blank_values=True)
            )
        elif _is_sensitive(raw_pair):
            legacy = True
        else:
            kept.append(raw_pair)
    if not members and not legacy:
        return query_string, [], False
    return "&".join(kept).encode("latin-1"), members, legacy


class MemberQueryScrubber:
    """Pure ASGI middleware that moves member query values into the scope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope.get("type") in {"http", "websocket"}:
            cleaned, members, legacy = split_member_query(scope.get("query_string", b""))
            if members or legacy:
                # Mutate in place: uvicorn logs from this very dict.
                scope["query_string"] = cleaned
                scope[MEMBER_SCOPE_KEY] = tuple(members)
                if legacy:
                    scope[LEGACY_ID_SCOPE_KEY] = True
        await self.app(scope, receive, send)


def _redact_text(value: str) -> str:
    path, separator, query = value.partition("?")
    if not separator:
        return value
    pairs = [
        f"{raw_pair.partition('=')[0]}=[REDACTED]"
        if _is_sensitive(raw_pair)
        else raw_pair
        for raw_pair in query.split("&")
    ]
    return f"{path}?{'&'.join(pairs)}"


class MemberQueryLogFilter(logging.Filter):
    """Defense in depth: redact member query values in any access-log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact_text(arg) if isinstance(arg, str) and "?" in arg else arg
                for arg in record.args
            )
        return True


def install_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, MemberQueryLogFilter) for item in logger.filters):
        logger.addFilter(MemberQueryLogFilter())
