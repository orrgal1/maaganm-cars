# MaaganM Cars API

The local FastAPI service runs on port 8002 and is exposed through `https://<gateway-host>/maaganm-cars/`. Sign in at `/login`; the gateway session cookie authorizes remote requests, and the gateway injects the bearer token from `local-api-gateway/.env`. The token is not configured in this service's `.env` or `.api_token` file.

Run the API manually with `./.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8002`. The workspace's `python3 manage.py install` manages this API as a LaunchAgent. There is no discovery responder in the current runtime. The older `run_mail_bus.py` command worker is retained for migration and rollback, but is not part of the current runtime.

For direct local REST calls, send the API bearer token; remote gateway calls use
the signed-in browser session cookie described above. Select `personal` or `work`
with the `account` query parameter (defaults to `personal`). The service reads
the selected upstream Cars code from `CARS_PERSONAL_CODE` or `CARS_WORK_CODE`
in its local `.env.local`, `.env`, or process environment, then uses it for the
upstream session. Never send these account codes through Instinct or expose them
in responses or logs. If the selected account has no local code configured, the
service returns a safe `503 credentials_missing` error. The health response's
`configured_accounts` field lists only accounts with a locally configured code;
health itself does not contact the upstream service.

For builder feedback, load `.agents/skills/builder-feedback/SKILL.md` in a fresh session. It verifies GitHub issue and comment authors before acting, and provides a 60-second poller.
