from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from security import APIException


_LOCAL_TIMEZONE = ZoneInfo("Asia/Jerusalem")


def _invalid_response() -> APIException:
    return APIException(
        status_code=502,
        code="invalid_schedule_response",
        message="The vehicle schedule service returned an invalid response",
    )


def _plain_text(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())


def _car_id(value: object) -> int | None:
    # The captured schedule contains one row per real car, identified by a
    # canonical positive decimal string. Rows without that stable identifier
    # cannot be treated as assignable vehicles.
    if (
        value is None
        or value == ""
        or value == "0"
        or (type(value) is int and value == 0)
    ):
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise _invalid_response()

    parsed = int(value)
    if parsed <= 0 or str(parsed) != value:
        raise _invalid_response()
    return parsed


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _invalid_response()
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise _invalid_response() from None
    if not math.isfinite(parsed):
        raise _invalid_response()
    return parsed


def _daily_cars(
    payload: object,
    expected_from: str,
    expected_to: str,
    expected_day_begin: float,
    requested_start: float,
    requested_end: float,
) -> dict[int, tuple[str, list[str], bool]]:
    if not isinstance(payload, dict):
        raise _invalid_response()

    options = payload.get("options")
    cars = payload.get("cars")
    if (
        not isinstance(options, dict)
        or options.get("date_from") != expected_from
        or options.get("date_to") != expected_to
        or _timestamp(options.get("day_begin")) != expected_day_begin
        or not isinstance(cars, list)
    ):
        raise _invalid_response()

    result: dict[int, tuple[str, list[str], bool]] = {}
    for car in cars:
        if not isinstance(car, dict):
            raise _invalid_response()

        car_id = _car_id(car.get("id"))
        if car_id is None:
            continue
        if car_id in result:
            raise _invalid_response()

        info = car.get("info")
        drives = car.get("drives")
        if (
            not isinstance(info, list)
            or not info
            or not all(isinstance(item, str) for item in info)
            or not isinstance(drives, list)
        ):
            raise _invalid_response()

        label = _plain_text(info[0])
        if not label:
            raise _invalid_response()
        details = [text for item in info[1:] if (text := _plain_text(item))]

        blocked = False
        for drive in drives:
            if not isinstance(drive, dict):
                raise _invalid_response()
            block_start = _timestamp(drive.get("sdr_start"))
            block_end = _timestamp(drive.get("sdr_end"))
            if block_end <= block_start:
                raise _invalid_response()
            if block_start < requested_end and block_end > requested_start:
                blocked = True

        result[car_id] = (label, details, blocked)

    return result


def _covered_local_dates(start_at: datetime, end_at: datetime) -> list[date]:
    start_local = start_at.astimezone(_LOCAL_TIMEZONE)
    end_local = end_at.astimezone(_LOCAL_TIMEZONE)

    stop = end_local.date()
    if end_local.time() != time.min:
        stop += timedelta(days=1)

    days: list[date] = []
    current = start_local.date()
    while current < stop:
        days.append(current)
        current += timedelta(days=1)
    return days


async def available_cars(
    client: httpx.AsyncClient,
    start_at: datetime,
    end_at: datetime,
) -> dict:
    """Return cars present and schedule-free throughout a half-open interval."""
    if (
        start_at.tzinfo is None
        or start_at.utcoffset() is None
        or end_at.tzinfo is None
        or end_at.utcoffset() is None
    ):
        raise APIException(
            status_code=400,
            code="invalid_schedule_interval",
            message="Schedule interval datetimes must include a UTC offset",
        )

    try:
        requested_start = start_at.astimezone(timezone.utc).timestamp()
        requested_end = end_at.astimezone(timezone.utc).timestamp()
    except (OverflowError, OSError, ValueError):
        raise APIException(
            status_code=400,
            code="invalid_schedule_interval",
            message="Schedule interval is outside the supported datetime range",
        ) from None

    if requested_start >= requested_end:
        raise APIException(
            status_code=400,
            code="invalid_schedule_interval",
            message="Schedule interval end must be after its start",
        )

    days = _covered_local_dates(start_at, end_at)
    cars: dict[int, dict] = {}

    for day_index, local_day in enumerate(days):
        date_prefix = local_day.isoformat()
        date_from = f"{date_prefix} 00:00:00"
        date_to = f"{date_prefix} 23:59:59"
        day_begin = datetime.combine(
            local_day, time.min, tzinfo=_LOCAL_TIMEZONE
        ).timestamp()
        try:
            response = await client.post(
                "/ajax.php",
                data={
                    "action": "get_schedule_drives",
                    "date_from": date_from,
                    "date_to": date_to,
                    "schedule_type": "sidur",
                },
            )
        except httpx.RequestError:
            raise APIException(
                status_code=502,
                code="schedule_upstream_unavailable",
                message="The vehicle schedule service is unavailable",
            ) from None

        if not 200 <= response.status_code < 300:
            raise APIException(
                status_code=502,
                code="schedule_upstream_failure",
                message="The vehicle schedule service rejected the request",
            )

        try:
            payload = response.json()
        except (UnicodeDecodeError, ValueError):
            raise _invalid_response() from None

        daily_cars = _daily_cars(
            payload,
            date_from,
            date_to,
            day_begin,
            requested_start,
            requested_end,
        )
        for car_id, (label, details, blocked) in daily_cars.items():
            state = cars.get(car_id)
            if state is None:
                cars[car_id] = {
                    "label": label,
                    "details": details,
                    "days": {day_index},
                    "blocked": blocked,
                }
                continue

            state["days"].add(day_index)
            state["blocked"] = state["blocked"] or blocked
            for detail in details:
                if detail not in state["details"]:
                    state["details"].append(detail)

    available = [
        {
            "id": car_id,
            "label": state["label"],
            "details": state["details"],
        }
        for car_id, state in sorted(cars.items())
        if len(state["days"]) == len(days) and not state["blocked"]
    ]

    return {
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "cars": available,
        "total": len(available),
        "source": "schedule",
        "assignment_guaranteed": False,
    }
