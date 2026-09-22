import asyncio
import copy
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, TypeVar

import config

from security import APIException, CarCredentials


T = TypeVar("T")
_DEFAULT_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class _StoredOutcome:
    value: Optional[Any] = None
    error_status: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def success(cls, value: Any) -> "_StoredOutcome":
        return cls(value=copy.deepcopy(value))

    @classmethod
    def failure(cls, exc: APIException) -> "_StoredOutcome":
        return cls(
            error_status=exc.status_code,
            error_code=exc.code,
            error_message=exc.message,
        )

    def unwrap(self) -> Any:
        if self.error_status is not None:
            assert self.error_code is not None and self.error_message is not None
            raise APIException(
                status_code=self.error_status,
                code=self.error_code,
                message=self.error_message,
            )
        return copy.deepcopy(self.value)


@dataclass(frozen=True)
class _CacheEntry:
    payload_hash: str
    outcome: _StoredOutcome
    expires_at: float


@dataclass(frozen=True)
class _InFlight:
    payload_hash: str
    task: asyncio.Task[_StoredOutcome]


def idempotency_scope(credentials: CarCredentials, operation: str) -> str:
    """Build an account, credential, and operation scope without retaining the code."""
    credential_fingerprint = hmac.new(
        config.JWT_SECRET.encode("utf-8"),
        credentials.code.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{credentials.account}:{credential_fingerprint}:{operation}"


def _payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class IdempotencyStore:
    """Serialize and retain keyed mutation outcomes for a bounded period."""

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._in_flight: dict[tuple[str, str], _InFlight] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        *,
        key: Optional[str],
        scope: str,
        payload: Mapping[str, Any],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """Run an unkeyed call normally, or deduplicate a keyed mutation."""
        if key is None:
            return await operation()

        request_hash = _payload_hash(payload)
        cache_key = (scope, key)

        async with self._lock:
            self._remove_expired_locked()

            cached = self._cache.get(cache_key)
            if cached is not None:
                self._ensure_matching_payload(cached.payload_hash, request_hash)
                return cached.outcome.unwrap()

            current = self._in_flight.get(cache_key)
            if current is not None:
                self._ensure_matching_payload(current.payload_hash, request_hash)
                task = current.task
            else:
                task = asyncio.create_task(
                    self._run_and_store(cache_key, request_hash, operation)
                )
                self._in_flight[cache_key] = _InFlight(request_hash, task)

        outcome = await asyncio.shield(task)
        return outcome.unwrap()

    async def _run_and_store(
        self,
        cache_key: tuple[str, str],
        request_hash: str,
        operation: Callable[[], Awaitable[T]],
    ) -> _StoredOutcome:
        try:
            try:
                outcome = _StoredOutcome.success(await operation())
            except APIException as exc:
                outcome = _StoredOutcome.failure(exc)
            except Exception:
                outcome = _StoredOutcome.failure(
                    APIException(
                        status_code=502,
                        code="upstream_outcome_unknown",
                        message=(
                            "The upstream service did not confirm the mutation outcome. "
                            "Reuse this Idempotency-Key to retrieve this result without "
                            "resubmitting the mutation."
                        ),
                    )
                )

            async with self._lock:
                self._cache[cache_key] = _CacheEntry(
                    payload_hash=request_hash,
                    outcome=outcome,
                    expires_at=time.monotonic() + self._ttl_seconds,
                )
            return outcome
        finally:
            async with self._lock:
                current = self._in_flight.get(cache_key)
                if current is not None and current.task is asyncio.current_task():
                    del self._in_flight[cache_key]

    def _remove_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [
            cache_key
            for cache_key, entry in self._cache.items()
            if entry.expires_at <= now
        ]
        for cache_key in expired:
            del self._cache[cache_key]

    @staticmethod
    def _ensure_matching_payload(stored_hash: str, request_hash: str) -> None:
        if stored_hash != request_hash:
            raise APIException(
                status_code=409,
                code="idempotency_conflict",
                message=(
                    "This Idempotency-Key was already used for a different request "
                    "within the same account and operation."
                ),
            )


idempotency_store = IdempotencyStore()
