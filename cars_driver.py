"""Asynchronous, source-backed driver for the Sidur booking service."""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

import httpx

import config
from idempotency import mark_mutation_started
from security import APIException, CarCredentials
from sidur_forms import (
    Booking,
    BookingFormState,
    api_datetime_to_form,
    extract_booking_form,
    extract_successful_controls,
    extract_visible_errors,
    is_authenticated_page,
    is_login_page,
    overlay_scalar_controls,
    parse_bookings,
    validate_datetime_range,
)

_CREATE_PATH = "/order.php"
_ORDERS_PATH = "/my_orders.php"
_LOGIN_PATH = "/login.php"
_ALLOWED_FIELDS = {
    "start_at",
    "end_at",
    "destination_id",
    "purpose_id",
    "comment",
    "dispatcher_comment",
    "car_preferences",
}
_REQUIRED_CREATE_FIELDS = {"start_at", "end_at", "destination_id", "purpose_id"}


def _api_error(status_code: int, code: str, message: str) -> APIException:
    return APIException(status_code=status_code, code=code, message=message)


def _invalid_booking(message: str) -> APIException:
    return _api_error(400, "invalid_booking", message)


def _upstream_unavailable() -> APIException:
    return _api_error(
        502,
        "upstream_unavailable",
        "The upstream booking service is unavailable.",
    )


def _invalid_upstream_response() -> APIException:
    return _api_error(
        502,
        "invalid_upstream_response",
        "The upstream booking service returned an invalid response.",
    )


def _member_capacity() -> APIException:
    return _api_error(
        503,
        "member_capacity",
        "Too many member sessions are active; retry shortly.",
    )


def _outcome_unknown() -> APIException:
    return _api_error(
        502,
        "upstream_outcome_unknown",
        "The upstream booking service did not confirm the mutation outcome.",
    )


def _origin(url: httpx.URL) -> Tuple[str, str, Optional[int]]:
    port = url.port
    if port is None:
        port = 443 if url.scheme.lower() == "https" else 80
    return (url.scheme.lower(), url.host.lower(), port)


def _aware_instant(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _same_instant(value: str, expected: datetime) -> bool:
    parsed = _aware_instant(value)
    return parsed is not None and parsed == expected.astimezone(timezone.utc)


def _credential_fingerprint(credentials: CarCredentials) -> Tuple[str, bytes]:
    digest = hashlib.sha256()
    digest.update(b"sidur-session\0")
    digest.update(credentials.account.encode("utf-8"))
    digest.update(b"\0")
    digest.update(credentials.code.encode("utf-8"))
    return credentials.account, digest.digest()


@dataclass
class _SessionState:
    client: httpx.AsyncClient
    lock: asyncio.Lock
    idle: asyncio.Event
    users: int = 0
    closed: bool = False
    discard: bool = False
    last_used: float = 0.0


@dataclass
class _MutationResult:
    response: Optional[httpx.Response]
    transport_failed: bool


class CarsDriver:
    def __init__(
        self, transport: Optional[httpx.AsyncBaseTransport] = None
    ) -> None:
        self._transport = transport
        self._base_url = httpx.URL(config.CARS_BASE_URL + "/")
        if self._base_url.scheme not in {"http", "https"} or not self._base_url.host:
            raise RuntimeError("CARS_BASE_URL must be an absolute HTTP(S) URL")
        self._base_origin = _origin(self._base_url)
        self._sessions: Dict[Tuple[str, bytes], _SessionState] = {}
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def close(self) -> None:
        """Stop accepting work, wait for reserved operations, and close clients."""
        async with self._close_lock:
            async with self._state_lock:
                self._closed = True
                states = list(self._sessions.values())

            for state in states:
                await state.idle.wait()
            acquired: List[_SessionState] = []
            try:
                for state in states:
                    await state.lock.acquire()
                    acquired.append(state)
                for state in states:
                    if not state.closed:
                        await state.client.aclose()
                        state.closed = True
            finally:
                for state in reversed(acquired):
                    state.lock.release()

            async with self._state_lock:
                self._sessions.clear()

    async def _guard_redirect(self, response: httpx.Response) -> None:
        location = response.headers.get("location")
        if not location or not 300 <= response.status_code < 400:
            return
        try:
            target = response.url.join(location)
        except Exception:
            await response.aclose()
            raise httpx.RequestError(
                "Invalid upstream redirect", request=response.request
            ) from None
        if _origin(target) != self._base_origin:
            await response.aclose()
            raise httpx.RequestError(
                "Cross-origin upstream redirect blocked", request=response.request
            )

    def _new_session_state(self) -> _SessionState:
        client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
            transport=self._transport,
            event_hooks={"response": [self._guard_redirect]},
        )
        idle = asyncio.Event()
        idle.set()
        return _SessionState(client=client, lock=asyncio.Lock(), idle=idle)

    async def _release_user(
        self,
        fingerprint: Tuple[str, bytes],
        state: _SessionState,
        discard: bool,
    ) -> None:
        close_client = False
        async with self._state_lock:
            state.users -= 1
            if discard:
                state.discard = True
            if state.users == 0:
                state.idle.set()
                if (
                    state.discard
                    and self._sessions.get(fingerprint) is state
                ):
                    del self._sessions[fingerprint]
                    state.closed = True
                    close_client = True
        if close_client:
            try:
                await state.client.aclose()
            except Exception:
                pass

    def _evict_idle_member_locked(self) -> Optional[_SessionState]:
        """Drop the least recently used idle member session; caller holds the lock."""
        idle = [
            (state.last_used, fingerprint)
            for fingerprint, state in self._sessions.items()
            if fingerprint[0] == "member" and state.users == 0
        ]
        if not idle:
            return None
        _, fingerprint = min(idle)
        state = self._sessions.pop(fingerprint)
        state.closed = True
        return state

    @asynccontextmanager
    async def _session(
        self, credentials: CarCredentials
    ) -> AsyncIterator[httpx.AsyncClient]:
        """Yield one authenticated client while serializing its credential scope."""
        if not credentials.code:
            raise _api_error(401, "invalid_credentials", "Car credentials are invalid.")
        fingerprint = _credential_fingerprint(credentials)
        evicted: Optional[_SessionState] = None
        async with self._state_lock:
            if self._closed:
                raise _upstream_unavailable()
            state = self._sessions.get(fingerprint)
            if state is None:
                if credentials.account == "member":
                    members = sum(1 for key in self._sessions if key[0] == "member")
                    if members >= config.CARS_MAX_MEMBER_SESSIONS:
                        evicted = self._evict_idle_member_locked()
                        if evicted is None:
                            raise _member_capacity()
                state = self._new_session_state()
                self._sessions[fingerprint] = state
            state.users += 1
            state.last_used = time.monotonic()
            state.idle.clear()
        if evicted is not None:
            try:
                await evicted.client.aclose()
            except Exception:
                pass

        acquired = False
        discard = False
        try:
            await state.lock.acquire()
            acquired = True
            if state.closed:
                raise _upstream_unavailable()
            await self._safe_get(state.client, credentials, _ORDERS_PATH)
            async with self._state_lock:
                state.discard = False
            yield state.client
        except APIException as exc:
            discard = exc.code == "invalid_credentials"
            raise
        finally:
            if acquired:
                state.lock.release()
            await self._release_user(fingerprint, state, discard)

    async def _request_get(
        self, client: httpx.AsyncClient, path: str
    ) -> httpx.Response:
        try:
            return await client.get(path)
        except httpx.RequestError:
            raise _upstream_unavailable() from None

    async def _authenticate(
        self, client: httpx.AsyncClient, credentials: CarCredentials
    ) -> None:
        encoded = urlencode(
            [
                ("carlog", credentials.code),
                ("mode", "sdr_login_type_auth_only"),
                ("location", _ORDERS_PATH),
            ]
        ).encode("utf-8")
        try:
            response = await client.post(
                _LOGIN_PATH,
                content=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise _upstream_unavailable() from None
        if response.status_code >= 500:
            raise _upstream_unavailable()
        if 400 <= response.status_code < 500:
            raise _api_error(
                401,
                "invalid_credentials",
                "The upstream booking service rejected the car credentials.",
            )

    async def _safe_get(
        self, client: httpx.AsyncClient, credentials: CarCredentials, path: str
    ) -> httpx.Response:
        """Perform a read, refreshing authentication no more than once."""
        response = await self._request_get(client, path)
        if response.status_code >= 500:
            raise _upstream_unavailable()
        needs_auth = response.status_code in {401, 403} or (
            response.status_code < 400 and is_login_page(response.content)
        )
        if needs_auth:
            await self._authenticate(client, credentials)
            response = await self._request_get(client, path)
        if response.status_code >= 500:
            raise _upstream_unavailable()
        if response.status_code in {401, 403} or is_login_page(response.content):
            raise _api_error(
                401,
                "invalid_credentials",
                "The upstream booking service rejected the car credentials.",
            )
        if not 200 <= response.status_code < 300:
            raise _upstream_unavailable()
        if not is_authenticated_page(response.content):
            raise _invalid_upstream_response()
        return response

    @staticmethod
    def _bookings(markup: bytes) -> List[Booking]:
        try:
            return parse_bookings(markup)
        except (TypeError, ValueError):
            raise _invalid_upstream_response() from None

    async def _list(self, client: httpx.AsyncClient, credentials: CarCredentials) -> List[Booking]:
        response = await self._safe_get(client, credentials, _ORDERS_PATH)
        return self._bookings(response.content)

    def _resolve_action(
        self, action: str, current_path: str, expected_path: str
    ) -> bool:
        try:
            target = self._base_url.join(action or current_path)
        except Exception:
            return False
        return (
            _origin(target) == self._base_origin
            and target.path == expected_path
            and not target.query
            and not target.fragment
        )

    def _extract_form(
        self,
        response: httpx.Response,
        selector: str,
        kind: str,
        expected_path: str,
        booking_id: Optional[int] = None,
    ) -> BookingFormState:
        try:
            state = extract_booking_form(response.content, selector)
        except (TypeError, ValueError):
            raise _invalid_upstream_response() from None
        if (
            state["kind"] != kind
            or not self._resolve_action(state["action"], response.url.path, expected_path)
            or (kind == "create" and state["booking_id"] is not None)
            or (booking_id is not None and state["booking_id"] != booking_id)
        ):
            raise _invalid_upstream_response()
        return state

    async def _create_form(
        self, client: httpx.AsyncClient, credentials: CarCredentials
    ) -> BookingFormState:
        response = await self._safe_get(client, credentials, _CREATE_PATH)
        return self._extract_form(
            response, "#regular_order", "create", _CREATE_PATH
        )

    async def _edit_form(
        self,
        client: httpx.AsyncClient,
        credentials: CarCredentials,
        booking_id: int,
    ) -> BookingFormState:
        response = await self._safe_get(
            client, credentials, "%s?mode=edit&ord_id=%d" % (_ORDERS_PATH, booking_id)
        )
        return self._extract_form(
            response,
            "#edit_order_form",
            "edit",
            _ORDERS_PATH,
            booking_id,
        )

    @staticmethod
    def _validate_fields(
        payload: Mapping[str, Any], *, require_create: bool
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise _invalid_booking("Booking data must be an object.")
        unknown = set(payload) - _ALLOWED_FIELDS
        if unknown:
            raise _invalid_booking("Booking data contains unknown fields.")
        if require_create:
            missing = _REQUIRED_CREATE_FIELDS - set(payload)
            if missing:
                raise _invalid_booking("Booking data is missing required fields.")
        elif not payload:
            raise _invalid_booking("At least one booking field must be supplied.")
        if any(value is None for value in payload.values()):
            raise _invalid_booking("Booking fields cannot be null.")

        result = dict(payload)
        if require_create:
            result.setdefault("comment", "")
            result.setdefault("dispatcher_comment", "")
            result.setdefault("car_preferences", {})
        for name in ("comment", "dispatcher_comment"):
            if name in result and not isinstance(result[name], str):
                raise _invalid_booking("Booking comments must be strings.")
        if "car_preferences" in result and not isinstance(
            result["car_preferences"], dict
        ):
            raise _invalid_booking("car_preferences must be an object.")
        for name in ("destination_id", "purpose_id"):
            if name in result and (
                isinstance(result[name], bool) or not isinstance(result[name], int)
            ):
                raise _invalid_booking("Booking option IDs must be integers.")
        return result

    @staticmethod
    def _numeric_options(
        options: Sequence[Mapping[str, Any]], *, allow_empty: bool = False
    ) -> Dict[int, Tuple[str, str]]:
        result: Dict[int, Tuple[str, str]] = {}
        for option in options:
            if option["disabled"]:
                continue
            raw = str(option["value"])
            if allow_empty and not raw:
                continue
            try:
                value = int(raw)
            except ValueError:
                raise _invalid_upstream_response() from None
            if value in result:
                raise _invalid_upstream_response()
            result[value] = (raw, str(option["label"]))
        return result

    @classmethod
    def _preference_options(
        cls, state: BookingFormState
    ) -> Tuple[Dict[str, str], Dict[str, Dict[int, str]], List[Dict[str, Any]]]:
        fields: Dict[str, str] = {}
        choices: Dict[str, Dict[int, str]] = {}
        public: List[Dict[str, Any]] = []
        for group in state["options"]["preferences"]:
            name = group["name"]
            if not name.startswith("grp_val_") or not name[len("grp_val_") :]:
                raise _invalid_upstream_response()
            public_id = name[len("grp_val_") :]
            if public_id in fields:
                raise _invalid_upstream_response()
            numeric = cls._numeric_options(group["choices"])
            fields[public_id] = name
            choices[public_id] = {value: raw for value, (raw, _) in numeric.items()}
            public.append(
                {
                    "id": public_id,
                    "label": group["label"],
                    "choices": [
                        {"value": value, "label": label}
                        for value, (_, label) in numeric.items()
                    ],
                }
            )
        return fields, choices, public

    @classmethod
    def _preference_overlay(
        cls, state: BookingFormState, supplied: Mapping[str, Any]
    ) -> Dict[str, str]:
        fields, choices, _ = cls._preference_options(state)
        overlay: Dict[str, str] = {}
        for public_id, value in supplied.items():
            if not isinstance(public_id, str) or public_id not in fields:
                raise _invalid_booking("A car preference ID is invalid.")
            if isinstance(value, bool) or not isinstance(value, int):
                raise _invalid_booking("A car preference value is invalid.")
            raw = choices[public_id].get(value)
            if raw is None:
                raise _invalid_booking("A car preference value is invalid.")
            overlay[fields[public_id]] = raw
        return overlay

    @classmethod
    def _form_values(
        cls, state: BookingFormState, payload: Mapping[str, Any]
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        start_value = payload.get("start_at", state["start_at"])
        end_value = payload.get("end_at", state["end_at"])
        try:
            start, end = validate_datetime_range(start_value, end_value)
            start_date, start_time, _ = api_datetime_to_form(start)
            end_date, end_time, _ = api_datetime_to_form(end)
        except (TypeError, ValueError):
            raise _invalid_booking(
                "Booking datetimes must be valid aware, minute-precision values with end after start."
            ) from None

        destinations = cls._numeric_options(state["options"]["destinations"], allow_empty=True)
        purposes = cls._numeric_options(state["options"]["purposes"], allow_empty=True)
        destination_id = payload.get("destination_id")
        if destination_id is None:
            try:
                destination_id = int(state["destination_id"])
            except ValueError:
                raise _invalid_upstream_response() from None
        purpose_id = payload.get("purpose_id")
        if purpose_id is None:
            try:
                purpose_id = int(state["purpose_id"])
            except ValueError:
                raise _invalid_upstream_response() from None
        if destination_id not in destinations or purpose_id not in purposes:
            raise _invalid_booking("A destination or purpose ID is invalid.")

        preference_values = payload.get("car_preferences", {})
        preference_overlay = cls._preference_overlay(state, preference_values)
        overlay = {
            "start_date": start_date,
            "start_time": start_time,
            "end_date": end_date,
            "end_time": end_time,
            "dest": destinations[destination_id][0],
            "drive_purpose": purposes[purpose_id][0],
            "comment": payload.get("comment", state["comment"]),
            "comment_to_sadran": payload.get(
                "dispatcher_comment", state["dispatcher_comment"]
            ),
        }
        overlay.update(preference_overlay)

        expected_preferences = dict(state["car_preferences"])
        expected_preferences.update(preference_overlay)
        expected = {
            "start_at": start,
            "end_at": end,
            "destination_id": destinations[destination_id][0],
            "destination_label": destinations[destination_id][1],
            "purpose_id": purposes[purpose_id][0],
            "purpose_label": purposes[purpose_id][1],
            "comment": overlay["comment"],
            "dispatcher_comment": overlay["comment_to_sadran"],
            "car_preferences": expected_preferences,
        }
        return overlay, expected

    async def _post_mutation(
        self,
        client: httpx.AsyncClient,
        path: str,
        controls: Sequence[Tuple[str, str]],
    ) -> _MutationResult:
        mark_mutation_started()
        encoded = urlencode(list(controls)).encode("utf-8")
        try:
            response = await client.post(
                path,
                content=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        except httpx.RequestError:
            return _MutationResult(response=None, transport_failed=True)
        return _MutationResult(response=response, transport_failed=False)

    @staticmethod
    def _raise_unconfirmed(result: _MutationResult) -> None:
        response = result.response
        if result.transport_failed or response is None:
            raise _outcome_unknown()
        if is_login_page(response.content):
            raise _outcome_unknown()
        if response.status_code in {307, 308} or response.status_code >= 500:
            raise _outcome_unknown()
        if 400 <= response.status_code < 500:
            raise _api_error(
                502,
                "upstream_rejected",
                "The upstream booking service rejected the mutation.",
            )
        if extract_visible_errors(response.content):
            raise _api_error(
                502,
                "upstream_rejected",
                "The upstream booking service rejected the mutation.",
            )
        raise _outcome_unknown()

    @staticmethod
    def _list_fields_match(booking: Booking, expected: Mapping[str, Any]) -> bool:
        return (
            _same_instant(booking["start_at"], expected["start_at"])
            and _same_instant(booking["end_at"], expected["end_at"])
            and booking["destination"] == expected["destination_label"]
            and booking["purpose"] == expected["purpose_label"]
        )

    @staticmethod
    def _form_matches(state: BookingFormState, expected: Mapping[str, Any]) -> bool:
        return (
            state["start_at"].astimezone(timezone.utc)
            == expected["start_at"].astimezone(timezone.utc)
            and state["end_at"].astimezone(timezone.utc)
            == expected["end_at"].astimezone(timezone.utc)
            and state["destination_id"] == expected["destination_id"]
            and state["purpose_id"] == expected["purpose_id"]
            and state["comment"] == expected["comment"]
            and state["dispatcher_comment"] == expected["dispatcher_comment"]
            and state["car_preferences"] == expected["car_preferences"]
        )

    async def get_options(self, credentials: CarCredentials) -> dict:
        async with self._session(credentials) as client:
            state = await self._create_form(client, credentials)
            destinations = self._numeric_options(
                state["options"]["destinations"], allow_empty=True
            )
            purposes = self._numeric_options(
                state["options"]["purposes"], allow_empty=True
            )
            _, _, preferences = self._preference_options(state)
            return {
                "destinations": [
                    {"id": value, "label": label}
                    for value, (_, label) in destinations.items()
                ],
                "purposes": [
                    {"id": value, "label": label}
                    for value, (_, label) in purposes.items()
                ],
                "car_preferences": preferences,
                "timezone": "Asia/Jerusalem",
                "assignment_mode": "automatic",
            }

    async def list_bookings(self, credentials: CarCredentials) -> dict:
        async with self._session(credentials) as client:
            bookings = await self._list(client, credentials)
            return {"bookings": bookings, "total": len(bookings)}

    async def create_booking(
        self, credentials: CarCredentials, payload: dict
    ) -> dict:
        validated = self._validate_fields(payload, require_create=True)
        try:
            validate_datetime_range(validated["start_at"], validated["end_at"])
        except (TypeError, ValueError):
            raise _invalid_booking(
                "Booking datetimes must be valid aware, minute-precision values with end after start."
            ) from None

        async with self._session(credentials) as client:
            before = await self._list(client, credentials)
            before_ids = {booking["id"] for booking in before}
            response = await self._safe_get(client, credentials, _CREATE_PATH)
            form = self._extract_form(response, "#regular_order", "create", _CREATE_PATH)
            overlay, expected = self._form_values(form, validated)
            controls = extract_successful_controls(
                response.content, "#regular_order", include_submit_order=True
            )
            if not any(name == "submit_order" for name, _ in controls):
                raise _invalid_upstream_response()
            controls = overlay_scalar_controls(controls, overlay)
            result = await self._post_mutation(client, _CREATE_PATH, controls)

            try:
                after = await self._list(client, credentials)
            except APIException:
                raise _outcome_unknown() from None
            candidates = [
                booking
                for booking in after
                if booking["id"] not in before_ids
                and self._list_fields_match(booking, expected)
            ]
            try:
                verified = [
                    booking
                    for booking in candidates
                    if self._form_matches(
                        await self._edit_form(client, credentials, booking["id"]),
                        expected,
                    )
                ]
            except APIException:
                raise _outcome_unknown() from None
            if len(verified) == 1:
                return {"booking": verified[0]}
            self._raise_unconfirmed(result)
            raise AssertionError("unreachable")

    async def update_booking(
        self, credentials: CarCredentials, booking_id: int, changes: dict
    ) -> dict:
        if isinstance(booking_id, bool) or not isinstance(booking_id, int) or booking_id <= 0:
            raise _invalid_booking("booking_id must be a positive integer.")
        validated = self._validate_fields(changes, require_create=False)

        async with self._session(credentials) as client:
            before = await self._list(client, credentials)
            if not any(booking["id"] == booking_id for booking in before):
                raise _api_error(404, "booking_not_found", "Booking was not found.")
            response = await self._safe_get(
                client,
                credentials,
                "%s?mode=edit&ord_id=%d" % (_ORDERS_PATH, booking_id),
            )
            form = self._extract_form(
                response, "#edit_order_form", "edit", _ORDERS_PATH, booking_id
            )
            overlay, expected = self._form_values(form, validated)
            controls = extract_successful_controls(
                response.content, "#edit_order_form", include_submit_order=True
            )
            if not any(name == "submit_order" for name, _ in controls):
                raise _invalid_upstream_response()
            controls = overlay_scalar_controls(controls, overlay)
            result = await self._post_mutation(client, _ORDERS_PATH, controls)

            try:
                after = await self._list(client, credentials)
                booking = next(
                    (item for item in after if item["id"] == booking_id), None
                )
                if booking is not None:
                    fresh_form = await self._edit_form(client, credentials, booking_id)
                else:
                    fresh_form = None
            except APIException:
                raise _outcome_unknown() from None
            if (
                booking is not None
                and fresh_form is not None
                and self._list_fields_match(booking, expected)
                and self._form_matches(fresh_form, expected)
            ):
                return {"booking": booking}
            self._raise_unconfirmed(result)
            raise AssertionError("unreachable")

    async def cancel_booking(
        self, credentials: CarCredentials, booking_id: int
    ) -> dict:
        if isinstance(booking_id, bool) or not isinstance(booking_id, int) or booking_id <= 0:
            raise _invalid_booking("booking_id must be a positive integer.")

        async with self._session(credentials) as client:
            before = await self._list(client, credentials)
            if not any(booking["id"] == booking_id for booking in before):
                raise _api_error(404, "booking_not_found", "Booking was not found.")
            response = await self._safe_get(
                client,
                credentials,
                "%s?mode=edit&ord_id=%d" % (_ORDERS_PATH, booking_id),
            )
            self._extract_form(
                response, "#edit_order_form", "edit", _ORDERS_PATH, booking_id
            )
            controls = extract_successful_controls(response.content, "#edit_order_form")
            controls = overlay_scalar_controls(controls, {"delete": "delete"})
            result = await self._post_mutation(client, _ORDERS_PATH, controls)

            try:
                after = await self._list(client, credentials)
            except APIException:
                raise _outcome_unknown() from None
            if not any(booking["id"] == booking_id for booking in after):
                return {"id": booking_id, "cancelled": True}
            self._raise_unconfirmed(result)
            raise AssertionError("unreachable")

    async def available_cars(
        self,
        credentials: CarCredentials,
        start_at: datetime,
        end_at: datetime,
    ) -> dict:
        async with self._session(credentials) as client:
            from sidur_schedule import available_cars

            return await available_cars(client, start_at, end_at)

    async def estimate_cost(
        self, credentials: CarCredentials, payload: dict
    ) -> dict:
        async with self._session(credentials) as client:
            from sidur_pricing import estimate_cost

            return await estimate_cost(client, payload)
