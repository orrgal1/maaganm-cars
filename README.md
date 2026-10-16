# MaaganM Cars API

The local FastAPI service runs on port 8002 and is exposed through `https://<gateway-host>/maaganm-cars/`. Sign in at `/login`; the gateway session cookie authorizes remote requests, and the gateway injects the bearer token from `local-api-gateway/.env`. The token is not configured in this service's `.env` or `.api_token` file.

Run the API manually with `./.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8002`. The workspace's `python3 manage.py install` manages this API as a LaunchAgent. There is no discovery responder in the current runtime. The older `run_mail_bus.py` command worker is retained for migration and rollback, but is not part of the current runtime.

For direct local REST calls, send the API bearer token; remote gateway calls use
the signed-in browser session cookie described above. Select `personal` or `work`
with the `account` query parameter (defaults to `personal`). The service reads
the selected upstream Cars code from `CARS_PERSONAL_CODE` or `CARS_WORK_CODE`
in its local `.env.local`, `.env`, or process environment, then uses it for the
upstream session. If the selected account has no local code configured, the
service returns a safe `503 credentials_missing` error.

To act as a specific kibbutz member, send that member's 4-digit Sidur code in
the `X-Sidur-Member` header (preferred). For compatibility, `member_id` is also
accepted as a query parameter on every endpoint and as a JSON body field on
`POST /bookings`, `PATCH /bookings/{id}`, and `POST /bookings/estimate`. A
member code takes precedence over `account`; without one, behavior is
unchanged. Anything other than exactly four ASCII digits (including an explicit
`"member_id": null`), or two different member codes in one request, returns
`400 invalid_member_id`. `DELETE /bookings/{id}` rejects any request body with
400, so use the header there. The `id` query parameter proposed in GitHub issue
#7, and case, dash, or bracket variants such as `Member_ID` or `member_id[]`,
are rejected with the same error. The Sidur site has no password, so the member
code is the whole upstream login; the owner chose to allow callers, including
Instinct, to send it. The service never logs, persists, or returns it: it strips
`member_id`, `id`, and their variants from the URL before routing, so uvicorn
access logs never contain them, and a log filter redacts them as a second layer.
Only derived values are kept in memory: an HMAC of the code inside the
idempotency scope for up to 24 hours, and a SHA-256 session fingerprint while
that member's session is cached. Each member code gets its own isolated
upstream session and cookie jar. At most `CARS_MAX_MEMBER_SESSIONS` (default
16) member sessions are cached; idle ones are evicted least-recently-used first,
and when all are busy the service returns a retryable `503 member_capacity`.
Failures before the upstream mutation is submitted are never cached under an
Idempotency-Key, so a retry with the same key runs again. The locally
configured account codes still never leave this machine.

The health response's `configured_accounts` field lists only accounts with a
locally configured code; it never reflects member codes, and health itself does
not contact the upstream service.

For builder feedback, load `.agents/skills/builder-feedback/SKILL.md` in a fresh session. It verifies GitHub issue and comment authors before acting, and provides a 60-second poller.
