import asyncio
import logging
import socket
import threading
import time
import unittest
import uuid
from contextlib import AsyncExitStack, ExitStack
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import uvicorn
from fastapi.testclient import TestClient
from starlette.requests import Request

import cars_driver as cars_driver_module
import config
import main
from cars_driver import CarsDriver
from idempotency import IdempotencyStore, mark_mutation_started
from member_redaction import MemberQueryLogFilter, split_member_query
from security import (
    APIException,
    CarCredentials,
    MEMBER_SCOPE_KEY,
    get_car_credentials,
    resolve_car_credentials,
    validate_member_code,
)

TOKEN = "test-static-token"
PERSONAL = "1111"
WORK = "2222"
MEMBER_A = "4821"
MEMBER_B = "7350"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BOOKING = {
    "id": 5,
    "start_at": "2026-10-12T08:00:00+03:00",
    "end_at": "2026-10-12T09:30:00+03:00",
    "destination": "x",
    "purpose": "y",
    "comment": "",
    "car_label": None,
    "status": "new",
}


def _request(scope_extra=None) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/bookings", "headers": []}
    scope.update(scope_extra or {})
    return Request(scope)



class _Patched(unittest.TestCase):
    """Patch auth and local codes; record every credential the driver receives."""

    def setUp(self):
        self.seen: list[CarCredentials] = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(config, "CARS_API_TOKEN", TOKEN))
        stack.enter_context(patch.object(config, "CARS_PERSONAL_CODE", PERSONAL))
        stack.enter_context(patch.object(config, "CARS_WORK_CODE", WORK))

        def recorder(result):
            async def method(credentials, *args):
                self.seen.append(credentials)
                self.driver_args = args
                return result
            return method

        driver = main.cars_driver
        stack.enter_context(patch.object(driver, "list_bookings", recorder({"bookings": [], "total": 0})))
        stack.enter_context(patch.object(driver, "create_booking", recorder({"booking": BOOKING})))
        stack.enter_context(patch.object(driver, "update_booking", recorder({"booking": BOOKING})))
        stack.enter_context(patch.object(driver, "cancel_booking", recorder({"id": 5, "cancelled": True})))
        self.client = TestClient(main.app, raise_server_exceptions=False)


class ValidationTests(unittest.TestCase):
    def test_accepts_exactly_four_ascii_digits(self):
        self.assertEqual(validate_member_code("4821"), "4821")
        self.assertEqual(validate_member_code(" 0042 "), "0042")

    def test_rejects_everything_else_without_echo(self):
        bad = ["", "   ", "482", "48210", "48a1", "48 21", "٤٨٢١",
               "４８２１", "4821\x00", "-482", "48.1", "0x12", None, 4821]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(APIException) as raised:
                validate_member_code(value)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(raised.exception.code, "invalid_member_id")
            if isinstance(value, str) and value.strip():
                self.assertNotIn(value.strip(), raised.exception.message)


class PrecedenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(config, "CARS_PERSONAL_CODE", PERSONAL))
        stack.enter_context(patch.object(config, "CARS_WORK_CODE", WORK))

    async def test_default_behavior_unchanged(self):
        personal = await get_car_credentials(_request(), account="personal")
        work = await get_car_credentials(_request(), account="work")
        self.assertEqual((personal.account, personal.code), ("personal", PERSONAL))
        self.assertEqual((work.account, work.code), ("work", WORK))

    async def test_member_code_wins_over_account(self):
        for account in ("personal", "work"):
            creds = await get_car_credentials(_request(), account=account, x_sidur_member=MEMBER_A)
            self.assertEqual((creds.account, creds.code), ("member", MEMBER_A))
        creds = await get_car_credentials(_request(), account="work", member_id=MEMBER_B)
        self.assertEqual((creds.account, creds.code), ("member", MEMBER_B))

    async def test_member_code_works_without_local_codes(self):
        with patch.object(config, "CARS_PERSONAL_CODE", ""):
            creds = await get_car_credentials(_request(), x_sidur_member=MEMBER_A)
        self.assertEqual(creds.code, MEMBER_A)

    async def test_scrubbed_query_values_are_used(self):
        creds = await get_car_credentials(_request({MEMBER_SCOPE_KEY: (MEMBER_B,)}))
        self.assertEqual(creds.code, MEMBER_B)

    def test_conflicting_sources_rejected_identical_accepted(self):
        with self.assertRaises(APIException) as raised:
            resolve_car_credentials("personal", (MEMBER_A, MEMBER_B))
        self.assertEqual(raised.exception.code, "invalid_member_id")
        self.assertNotIn(MEMBER_A, raised.exception.message)
        self.assertNotIn(MEMBER_B, raised.exception.message)
        self.assertEqual(resolve_car_credentials("personal", (MEMBER_A, f" {MEMBER_A}")).code, MEMBER_A)

    def test_repr_never_shows_code(self):
        creds = resolve_car_credentials("personal", (MEMBER_A,))
        self.assertNotIn(MEMBER_A, repr(creds))


class EndpointTests(_Patched):
    def test_default_account_unchanged_over_http(self):
        self.assertEqual(self.client.get("/bookings", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get("/bookings?account=work", headers=AUTH).status_code, 200)
        self.assertEqual([(c.account, c.code) for c in self.seen], [("personal", PERSONAL), ("work", WORK)])

    def test_header_query_and_body_reach_driver(self):
        h = {**AUTH, "X-Sidur-Member": MEMBER_A}
        self.assertEqual(self.client.get("/bookings?account=work", headers=h).status_code, 200)
        self.assertEqual(self.client.get(f"/bookings?member_id={MEMBER_B}", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.delete("/bookings/5", headers=h).status_code, 200)
        body = {"start_at": BOOKING["start_at"], "end_at": BOOKING["end_at"],
                "destination_id": 1, "purpose_id": 1, "member_id": MEMBER_B}
        response = self.client.post("/bookings", headers=AUTH, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("member_id", self.driver_args[0])
        response = self.client.patch("/bookings/5", headers=h, json={"comment": "x", "member_id": MEMBER_A})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.driver_args[1], {"comment": "x"})
        self.assertEqual([c.code for c in self.seen], [MEMBER_A, MEMBER_B, MEMBER_A, MEMBER_B, MEMBER_A])
        self.assertTrue(all(c.account == "member" for c in self.seen))

    def test_invalid_and_conflicting_return_400_without_echo(self):
        cases = [
            ("get", "/bookings", {"X-Sidur-Member": "48a1"}, None),
            ("get", "/bookings?member_id=482", {}, None),
            ("get", "/bookings?member_id=", {}, None),
            ("get", f"/bookings?member_id={MEMBER_B}", {"X-Sidur-Member": MEMBER_A}, None),
            ("get", f"/bookings?member_id={MEMBER_A}&member_id={MEMBER_B}", {}, None),
            ("get", f"/bookings?id={MEMBER_A}", {}, None),
            ("get", f"/bookings?Member_ID={MEMBER_A}", {}, None),
            ("get", f"/bookings?member_id[]={MEMBER_A}", {}, None),
            ("get", f"/bookings?MEMBER-ID={MEMBER_A}", {}, None),
            ("get", f"/bookings?ID={MEMBER_A}", {}, None),
            ("post", "/bookings", {}, {"start_at": BOOKING["start_at"], "end_at": BOOKING["end_at"],
                                       "destination_id": 1, "purpose_id": 1, "member_id": None}),
            ("patch", "/bookings/5", {}, {"comment": "x", "member_id": None}),
            ("post", "/bookings/estimate", {}, {"start_at": BOOKING["start_at"], "end_at": BOOKING["end_at"],
                                                "distance_km": 3, "member_id": None}),
            ("patch", "/bookings/5", {"X-Sidur-Member": MEMBER_A}, {"comment": "x", "member_id": MEMBER_B}),
        ]
        for method, url, headers, body in cases:
            with self.subTest(url=url, headers=headers):
                kwargs = {"headers": {**AUTH, **headers}}
                if body is not None:
                    kwargs["json"] = body
                response = getattr(self.client, method)(url, **kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_member_id")
                self.assertNotIn(MEMBER_A, response.text)
                self.assertNotIn(MEMBER_B, response.text)
        self.assertEqual(self.seen, [])

    def test_delete_rejects_any_body(self):
        for content, code in [(f'{{"member_id": "{MEMBER_A}"}}', "invalid_member_id"),
                              ('{"other": 1}', "invalid_input")]:
            with self.subTest(code=code):
                response = self.client.request("DELETE", "/bookings/5", content=content,
                                               headers={**AUTH, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn(MEMBER_A, response.text)
        self.assertEqual(self.seen, [])
        self.assertEqual(self.client.delete("/bookings/5", headers=AUTH).status_code, 200)
        self.assertEqual([(c.account, c.code) for c in self.seen], [("personal", PERSONAL)])

    def test_members_sharing_an_idempotency_key_get_separate_results(self):
        key = f"shared-{uuid.uuid4()}"
        body = {"start_at": BOOKING["start_at"], "end_at": BOOKING["end_at"],
                "destination_id": 1, "purpose_id": 1}
        calls = []

        async def create(credentials, payload):
            calls.append(credentials.code)
            return {"booking": {**BOOKING, "id": len(calls), "comment": credentials.code}}

        with patch.object(main.cars_driver, "create_booking", create):
            results = [
                self.client.post("/bookings", json=body,
                                 headers={**AUTH, "Idempotency-Key": key, "X-Sidur-Member": member})
                for member in (MEMBER_A, MEMBER_B, MEMBER_A, MEMBER_B)
            ]
        self.assertEqual(calls, [MEMBER_A, MEMBER_B])
        ids = [r.json()["booking"]["id"] for r in results]
        self.assertEqual(ids, [1, 2, 1, 2])

    def test_pre_mutation_failure_is_not_cached_under_key(self):
        key = f"retry-{uuid.uuid4()}"
        calls = []

        async def cancel(credentials, booking_id):
            calls.append(credentials.code)
            if len(calls) == 1:
                raise APIException(503, "member_capacity", "busy")
            return {"id": booking_id, "cancelled": True}

        headers = {**AUTH, "Idempotency-Key": key, "X-Sidur-Member": MEMBER_A}
        with patch.object(main.cars_driver, "cancel_booking", cancel):
            first = self.client.delete("/bookings/5", headers=headers)
            second = self.client.delete("/bookings/5", headers=headers)
            third = self.client.delete("/bookings/5", headers=headers)
        self.assertEqual([first.status_code, second.status_code, third.status_code], [503, 200, 200])
        self.assertEqual(calls, [MEMBER_A, MEMBER_A])

    def test_member_only_patch_is_still_an_empty_update(self):
        response = self.client.patch("/bookings/5", headers=AUTH, json={"member_id": MEMBER_A})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.seen, [])

    def test_member_requires_gateway_bearer(self):
        response = self.client.get("/bookings", headers={"X-Sidur-Member": MEMBER_A})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.seen, [])

    def test_health_never_reflects_member_code(self):
        response = self.client.get(f"/health?member_id={MEMBER_A}",
                                   headers={**AUTH, "X-Sidur-Member": MEMBER_B})
        self.assertEqual(response.json(), {"status": "ok", "configured_accounts": ["personal", "work"]})

    def test_openapi_documents_header_and_param(self):
        schema = self.client.get("/openapi.json").json()
        params = {(p["in"], p["name"]) for p in schema["paths"]["/bookings"]["get"]["parameters"]}
        self.assertIn(("header", "X-Sidur-Member"), params)
        self.assertIn(("query", "member_id"), params)
        self.assertIn(("query", "account"), params)

    def test_errors_never_log_member_code(self):
        async def boom(credentials):
            raise RuntimeError("upstream exploded")

        with patch.object(main.cars_driver, "list_bookings", boom), \
                self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("probe").debug("start")
            response = self.client.get(f"/bookings?member_id={MEMBER_A}&account=work",
                                       headers={**AUTH, "X-Sidur-Member": MEMBER_A})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(MEMBER_A, response.text)
        # The test's own httpx client logs the URL it sent; only server-side
        # records matter here.
        server_side = [line for line in captured.output if ":httpx:" not in line]
        self.assertTrue(any("maaganm_cars.api" in line for line in server_side))
        self.assertNotIn(MEMBER_A, "\n".join(server_side))


class RedactionTests(unittest.TestCase):
    def test_split_member_query(self):
        cleaned, members, legacy = split_member_query(
            f"account=work&member_id={MEMBER_A}&member%5Fid={MEMBER_B}&x=1".encode())
        self.assertEqual(parse_qs(cleaned.decode()), {"account": ["work"], "x": ["1"]})
        self.assertEqual(members, [MEMBER_A, MEMBER_B])
        self.assertFalse(legacy)
        self.assertEqual(split_member_query(b"account=work"), (b"account=work", [], False))
        cleaned, _, legacy = split_member_query(f"id={MEMBER_A}".encode())
        self.assertEqual((cleaned, legacy), (b"", True))
        for variant in ("Member_ID", "member_id[]", "member_id%5B0%5D", "MEMBER-ID", "ID", "id[x]"):
            with self.subTest(variant=variant):
                cleaned, members, legacy = split_member_query(f"a=1&{variant}={MEMBER_A}".encode())
                self.assertEqual((cleaned, members, legacy), (b"a=1", [], True))

    def test_log_filter_redacts_access_record(self):
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1:1", "GET", f"/bookings?id={MEMBER_B}&Member_ID[]={MEMBER_A}", "1.1", 200), None)
        MemberQueryLogFilter().filter(record)
        self.assertNotIn(MEMBER_A, record.getMessage())
        self.assertNotIn(MEMBER_B, record.getMessage())


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class UvicornAccessLogTests(_Patched):
    """Run the real uvicorn server (default log config) and inspect access logs."""

    def test_access_log_never_contains_member_query(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port,
                                               access_log=True, lifespan="off"))
        capture = _Capture()
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.addHandler(capture)
        self.addCleanup(access_logger.removeHandler, capture)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(server.started)
        try:
            base = f"http://127.0.0.1:{port}"
            ok = httpx.get(f"{base}/bookings?account=work&member_id={MEMBER_A}", headers=AUTH)
            bad = httpx.get(f"{base}/bookings?member_id=48a1x", headers=AUTH)
            legacy = httpx.get(f"{base}/bookings?id={MEMBER_B}", headers=AUTH)
            variant = httpx.get(f"{base}/bookings?Member_ID[]={MEMBER_B}", headers=AUTH)
            header = httpx.get(f"{base}/bookings", headers={**AUTH, "X-Sidur-Member": MEMBER_B})
        finally:
            server.should_exit = True
            thread.join(10)
        self.assertEqual([ok.status_code, bad.status_code, legacy.status_code,
                          variant.status_code, header.status_code],
                         [200, 400, 400, 400, 200])
        self.assertEqual([c.code for c in self.seen], [MEMBER_A, MEMBER_B])
        joined = "\n".join(capture.lines)
        self.assertEqual(len(capture.lines), 5, joined)
        self.assertIn("/bookings?account=work", joined)
        for secret in (MEMBER_A, MEMBER_B, "48a1x", "member_id", "Member_ID", "id="):
            self.assertNotIn(secret, joined)


def _fake_upstream():
    """Sidur stand-in: carlog=<code> sets a per-code cookie; pages echo the cookie owner."""
    log: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        if request.method == "POST" and request.url.path == "/login.php":
            code = parse_qs(request.content.decode())["carlog"][0]
            log.append(("login", code, cookie))
            return httpx.Response(302, headers={"location": "/my_orders.php",
                                                "set-cookie": f"sid=s-{code}; Path=/"})
        log.append(("get", request.url.path, cookie))
        if cookie.startswith("sid=s-"):
            return httpx.Response(200, content=f"AUTH:{cookie[6:]}".encode())
        return httpx.Response(200, content=b"LOGIN")

    return httpx.MockTransport(handler), log


class SessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(cars_driver_module, "is_login_page", lambda c: c == b"LOGIN"))
        stack.enter_context(patch.object(cars_driver_module, "is_authenticated_page", lambda c: c.startswith(b"AUTH:")))
        transport, self.log = _fake_upstream()
        self.driver = CarsDriver(transport=transport)
        self.addAsyncCleanup(self.driver.close)

    async def _whoami(self, creds):
        async with self.driver._session(creds) as client:
            await asyncio.sleep(0.01)
            return (await client.get("/my_orders.php")).content.decode(), client

    def _member(self, code):
        return CarCredentials(account="member", code=code)

    async def test_each_member_gets_own_session_and_cookies(self):
        results = await asyncio.gather(*[
            self._whoami(self._member(code))
            for code in (MEMBER_A, MEMBER_B, MEMBER_A, MEMBER_B)
        ])
        self.assertEqual([r[0] for r in results], [f"AUTH:{MEMBER_A}", f"AUTH:{MEMBER_B}"] * 2)
        self.assertIs(results[0][1], results[2][1])
        self.assertIsNot(results[0][1], results[1][1])
        logins = [entry for entry in self.log if entry[0] == "login"]
        self.assertEqual(sorted(code for _, code, _ in logins), [MEMBER_A, MEMBER_B])
        for _, code, cookie in logins:
            self.assertEqual(cookie, "", "login request carried another member's cookie")

    async def test_member_never_shares_local_account_session(self):
        personal, client_p = await self._whoami(CarCredentials(account="personal", code=MEMBER_A))
        member, client_m = await self._whoami(self._member(MEMBER_A))
        self.assertEqual(personal, member)
        self.assertIsNot(client_p, client_m)
        self.assertEqual(len(self.driver._sessions), 2)

    async def test_member_sessions_are_bounded_with_lru_eviction(self):
        with patch.object(config, "CARS_MAX_MEMBER_SESSIONS", 2):
            await self._whoami(CarCredentials(account="personal", code=PERSONAL))
            _, first = await self._whoami(self._member("0001"))
            await self._whoami(self._member("0002"))
            await self._whoami(self._member("0001"))  # refresh: 0002 becomes LRU
            _, third = await self._whoami(self._member("0003"))
            members = [key for key in self.driver._sessions if key[0] == "member"]
            self.assertEqual(len(members), 2)
            self.assertEqual(len(self.driver._sessions), 3)
            who, again = await self._whoami(self._member("0001"))
            self.assertIs(again, first)
            self.assertFalse(first.is_closed)

    async def test_capacity_exhausted_returns_503(self):
        with patch.object(config, "CARS_MAX_MEMBER_SESSIONS", 2):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(self.driver._session(self._member("0001")))
                await stack.enter_async_context(self.driver._session(self._member("0002")))
                with self.assertRaises(APIException) as raised:
                    async with self.driver._session(self._member("0003")):
                        pass
                self.assertEqual((raised.exception.status_code, raised.exception.code),
                                 (503, "member_capacity"))
                async with self.driver._session(CarCredentials(account="personal", code=PERSONAL)):
                    pass

    async def test_evicted_client_is_closed(self):
        with patch.object(config, "CARS_MAX_MEMBER_SESSIONS", 1):
            _, first = await self._whoami(self._member("0001"))
            _, second = await self._whoami(self._member("0002"))
        self.assertTrue(first.is_closed)
        self.assertFalse(second.is_closed)

    async def test_capacity_503_retry_with_same_key_reaches_driver(self):
        store = IdempotencyStore()
        calls = []

        async def operation():
            calls.append(1)
            async with self.driver._session(self._member("0003")):
                mark_mutation_started()
                return {"ok": len(calls)}

        with patch.object(config, "CARS_MAX_MEMBER_SESSIONS", 2):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(self.driver._session(self._member("0001")))
                await stack.enter_async_context(self.driver._session(self._member("0002")))
                with self.assertRaises(APIException) as raised:
                    await store.execute(key="k", scope="s", payload={}, operation=operation)
                self.assertEqual(raised.exception.code, "member_capacity")
            result = await store.execute(key="k", scope="s", payload={}, operation=operation)
            again = await store.execute(key="k", scope="s", payload={}, operation=operation)
        self.assertEqual((result, again, len(calls)), ({"ok": 2}, {"ok": 2}, 2))

    async def test_failure_after_mutation_started_is_still_cached(self):
        store = IdempotencyStore()
        calls = []

        async def operation():
            calls.append(1)
            mark_mutation_started()
            raise APIException(502, "upstream_outcome_unknown", "unknown")

        for _ in range(2):
            with self.assertRaises(APIException):
                await store.execute(key="k", scope="s", payload={}, operation=operation)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
