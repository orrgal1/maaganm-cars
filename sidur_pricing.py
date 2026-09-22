"""Local cost estimates derived only from tariffs in owned Sidur drive history."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from security import APIException


_JERUSALEM = ZoneInfo("Asia/Jerusalem")
_CENT = Decimal("0.01")
_SECONDS_PER_HOUR = Decimal(3600)
_MAX_DETAIL_LOOKUPS = 24
_MAX_INTERVAL_DAYS = 366
_SPACE_RE = re.compile(r"\s+")
_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")
_MONEY_RE = re.compile(r"(?:₪)?([0-9]+(?:\.[0-9]+)?)\Z")
_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_PLATE_RE = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{3}(?![0-9])")
_WEEKDAYS = {
    "יום שני": 0,
    "יום שלישי": 1,
    "יום רביעי": 2,
    "יום חמישי": 3,
    "יום שישי": 4,
    "שבת": 5,
    "יום ראשון": 6,
}


@dataclass(frozen=True)
class _HistoryEntry:
    booking_id: int
    start_at: datetime
    end_at: datetime
    car_key: str

    @property
    def weekdays(self) -> Set[int]:
        return set(_covered_local_dates(self.start_at, self.end_at, unavailable=False))


@dataclass(frozen=True)
class _Band:
    start_minute: int
    end_minute: int
    hourly_rate: Decimal


@dataclass(frozen=True)
class _TariffDetail:
    booking_id: int
    tariff_name: str
    distance_rate: Decimal
    bands: Mapping[int, Tuple[_Band, ...]]
    car_key: str


class _UnusableTariff(Exception):
    pass


def _invalid_response() -> APIException:
    return APIException(
        status_code=502,
        code="invalid_pricing_response",
        message="The vehicle billing service returned an invalid response",
    )


def _tariff_unavailable(message: str) -> APIException:
    return APIException(status_code=409, code="tariff_unavailable", message=message)


def _clean_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()


def _label(value: str) -> str:
    return _clean_text(value).rstrip(":").strip()


def _value_after(label: Tag) -> str:
    values: List[str] = []
    for sibling in label.next_siblings:
        if isinstance(sibling, NavigableString):
            values.append(str(sibling))
        elif isinstance(sibling, Tag):
            values.append(sibling.get_text(" ", strip=True))
    return _clean_text(" ".join(values))


def _labeled_values(container: Tag, selector: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for label_tag in container.select(selector):
        if not isinstance(label_tag, Tag):
            raise _invalid_response()
        key = _label(label_tag.get_text(" ", strip=True))
        parent = label_tag.parent
        if not key or not isinstance(parent, Tag) or key in result:
            raise _invalid_response()
        result[key] = _value_after(label_tag)
    return result


def _positive_integer(value: str) -> int:
    normalized = _clean_text(value)
    if not _POSITIVE_INTEGER_RE.fullmatch(normalized):
        raise _invalid_response()
    return int(normalized)


def _decimal(value: str, *, allow_percent: bool = False) -> Decimal:
    normalized = _clean_text(value).replace(",", "").replace(" ", "")
    if allow_percent and normalized.endswith("%"):
        normalized = normalized[:-1]
    match = _MONEY_RE.fullmatch(normalized)
    if match is None:
        raise _invalid_response()
    try:
        parsed = Decimal(match.group(1))
    except InvalidOperation:
        raise _invalid_response() from None
    if not parsed.is_finite() or parsed < 0:
        raise _invalid_response()
    return parsed


def _source_datetime(value: str) -> datetime:
    try:
        parsed = datetime.strptime(_clean_text(value), "%d-%m-%y %H:%M")
    except ValueError:
        raise _invalid_response() from None
    return parsed.replace(tzinfo=_JERUSALEM)


def _car_key(value: str) -> str:
    normalized = _clean_text(value)
    plate = _PLATE_RE.search(normalized)
    return plate.group(0) if plate is not None else normalized.casefold()


def _history_link_id(href: object) -> Optional[int]:
    if not isinstance(href, str):
        return None
    parsed = urlsplit(href)
    if parsed.path != "/display_cost_details.php" or parsed.fragment:
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) != {"ord_id"} or len(query["ord_id"]) != 1:
        return None
    value = query["ord_id"][0]
    if not _POSITIVE_INTEGER_RE.fullmatch(value):
        return None
    return int(value)


def _history_car(card: Tag) -> str:
    values: List[str] = []
    for row in card.select("div.table_small"):
        if not isinstance(row, Tag):
            continue
        heading = row.select_one(".right_column")
        value = row.select_one(".left_column")
        if (
            isinstance(heading, Tag)
            and isinstance(value, Tag)
            and _label(heading.get_text(" ", strip=True)) == "רכב"
        ):
            normalized = _clean_text(value.get_text(" ", strip=True))
            if normalized:
                values.append(_car_key(normalized))
    if not values or any(value != values[0] for value in values[1:]):
        raise _invalid_response()
    return values[0]


def _history_entries(markup: str) -> List[_HistoryEntry]:
    document = BeautifulSoup(markup, "html.parser")
    content = document.select("#content_table")
    if len(content) != 1:
        raise _invalid_response()

    cards = content[0].select("div.details-card-wrapper.desktop_line")
    if not cards:
        cards = content[0].select("div.details-card-wrapper")
    if not cards:
        raise _invalid_response()

    entries: Dict[int, _HistoryEntry] = {}
    for card in cards:
        if not isinstance(card, Tag):
            raise _invalid_response()
        classes = set(str(value) for value in card.get("class", []))
        if not ({"drv_mode__my_order", "drv_mode__im_charged"} & classes):
            continue

        order_nodes = card.select(".order_id")
        if len(order_nodes) != 1:
            raise _invalid_response()
        booking_id = _positive_integer(order_nodes[0].get_text(" ", strip=True))

        linked_ids = {
            linked
            for anchor in card.select("a[href]")
            if (linked := _history_link_id(anchor.get("href"))) is not None
        }
        if linked_ids != {booking_id}:
            raise _invalid_response()

        timestamps = card.select(".times .sort_by_me")
        if len(timestamps) != 2:
            raise _invalid_response()
        raw_timestamps = [
            _positive_integer(node.get_text(" ", strip=True)) for node in timestamps
        ]
        try:
            start_at = datetime.fromtimestamp(raw_timestamps[0], tz=_JERUSALEM)
            end_at = datetime.fromtimestamp(raw_timestamps[1], tz=_JERUSALEM)
        except (OverflowError, OSError, ValueError):
            raise _invalid_response() from None
        if end_at.astimezone(timezone.utc) <= start_at.astimezone(timezone.utc):
            raise _invalid_response()

        entry = _HistoryEntry(
            booking_id=booking_id,
            start_at=start_at,
            end_at=end_at,
            car_key=_history_car(card),
        )
        previous = entries.get(booking_id)
        if previous is not None and previous != entry:
            raise _invalid_response()
        entries[booking_id] = entry

    if not entries:
        raise _invalid_response()
    return sorted(entries.values(), key=lambda entry: entry.start_at.timestamp(), reverse=True)


def _unique_section(document: BeautifulSoup, heading: str) -> Tag:
    matches: List[Tag] = []
    for section in document.select("div.more-tbl"):
        if not isinstance(section, Tag):
            continue
        title = section.find("h4", recursive=False)
        if isinstance(title, Tag) and _clean_text(title.get_text(" ", strip=True)) == heading:
            matches.append(section)
    if len(matches) != 1:
        raise _invalid_response()
    return matches[0]


def _direct_rows(section: Tag) -> List[Tag]:
    return [
        child
        for child in section.find_all("div", class_="more-tr", recursive=False)
        if isinstance(child, Tag)
    ]


def _row_fields(row: Tag) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for cell in row.find_all("div", class_="more-td", recursive=False):
        if not isinstance(cell, Tag):
            raise _invalid_response()
        label_tag = cell.find("span", recursive=False)
        if label_tag is None:
            if _clean_text(cell.get_text(" ", strip=True)):
                raise _invalid_response()
            continue
        key = _label(label_tag.get_text(" ", strip=True))
        if not key or key in fields:
            raise _invalid_response()
        fields[key] = _value_after(label_tag)
    return fields


def _minute(value: str, *, end: bool, start_minute: int = 0) -> int:
    match = _TIME_RE.fullmatch(_clean_text(value))
    if match is None:
        raise _invalid_response()
    minute = int(match.group(1)) * 60 + int(match.group(2))
    if end and minute == 0 and start_minute > 0:
        return 24 * 60
    return minute


def _parse_bands(rate_blocks: Sequence[Tag]) -> Mapping[int, Tuple[_Band, ...]]:
    by_weekday: Dict[int, List[_Band]] = {}
    for block in rate_blocks:
        fields = _labeled_values(block, ":scope > div > span")
        if set(fields) != {"יום בשבוע", "התחלה", "סיום", "לשעה"}:
            raise _invalid_response()
        weekday = _WEEKDAYS.get(fields["יום בשבוע"])
        if weekday is None:
            raise _invalid_response()
        start = _minute(fields["התחלה"], end=False)
        end = _minute(fields["סיום"], end=True, start_minute=start)
        rate = _decimal(fields["לשעה"])
        if end <= start:
            raise _invalid_response()
        by_weekday.setdefault(weekday, []).append(_Band(start, end, rate))

    if not by_weekday:
        raise _UnusableTariff("the tariff has no explicit weekday time bands")

    result: Dict[int, Tuple[_Band, ...]] = {}
    for weekday, bands in by_weekday.items():
        ordered = sorted(bands, key=lambda band: (band.start_minute, band.end_minute))
        cursor = 0
        for band in ordered:
            if band.start_minute != cursor:
                raise _invalid_response()
            cursor = band.end_minute
        if cursor != 24 * 60:
            raise _invalid_response()
        result[weekday] = tuple(ordered)
    return result


def _detail(markup: str, expected_booking_id: int) -> _TariffDetail:
    document = BeautifulSoup(markup, "html.parser")
    drive_tables = document.select("table.drive_details")
    if len(drive_tables) != 1 or not isinstance(drive_tables[0], Tag):
        raise _invalid_response()
    facts = _labeled_values(drive_tables[0], "div > label")
    required_facts = {"מספר הזמנה", "תחילת נסיעה", "גמר נסיעה", "רכב"}
    if not required_facts.issubset(facts):
        raise _invalid_response()
    if _positive_integer(facts["מספר הזמנה"]) != expected_booking_id:
        raise _invalid_response()
    if not facts["תחילת נסיעה"] or not facts["גמר נסיעה"]:
        raise _UnusableTariff("the source is not a completed billed trip")
    trip_start = _source_datetime(facts["תחילת נסיעה"])
    trip_end = _source_datetime(facts["גמר נסיעה"])
    if trip_end.astimezone(timezone.utc) <= trip_start.astimezone(timezone.utc):
        raise _invalid_response()
    if not facts["רכב"]:
        raise _invalid_response()

    share_rows = _direct_rows(_unique_section(document, "חלקך בעלות הנסיעה"))
    if len(share_rows) != 1:
        raise _UnusableTariff("the source has multiple or missing charge shares")
    share = _row_fields(share_rows[0])
    required_share = {"חלק", "מ", "עד", 'ק"מ', "תעריף", "חלק בחיוב"}
    if not required_share.issubset(share):
        raise _invalid_response()
    _positive_integer(share["חלק"])
    share_start = _source_datetime(share["מ"])
    share_end = _source_datetime(share["עד"])
    if (
        share_start != trip_start
        or share_end != trip_end
        or share_end.astimezone(timezone.utc) <= share_start.astimezone(timezone.utc)
    ):
        raise _UnusableTariff("the source does not describe one complete trip charge")
    _decimal(share['ק"מ'])
    share_percent = _decimal(share["חלק בחיוב"], allow_percent=True)
    if share_percent != Decimal(100):
        raise _UnusableTariff("the source is not a 100 percent charge share")
    tariff_name = _clean_text(share["תעריף"])
    if not tariff_name:
        raise _UnusableTariff("the source has no named historical tariff")

    tariff_rows = _direct_rows(_unique_section(document, "תעריפים"))
    if len(tariff_rows) != 1:
        raise _UnusableTariff("the source has multiple or missing tariff definitions")
    cells = [
        cell
        for cell in tariff_rows[0].find_all("div", class_="more-td", recursive=False)
        if isinstance(cell, Tag)
    ]
    if len(cells) != 3:
        raise _invalid_response()
    parsed_name = _clean_text(cells[0].get_text(" ", strip=True))
    if not parsed_name:
        raise _UnusableTariff("the source tariff definition is blank")
    if parsed_name != tariff_name:
        raise _invalid_response()

    distance_fields = _labeled_values(cells[1], ":scope > span")
    if set(distance_fields) != {"עלות מרחק"}:
        raise _invalid_response()
    distance_rate = _decimal(distance_fields["עלות מרחק"])

    time_label = cells[2].find("span", recursive=False)
    if not isinstance(time_label, Tag) or _label(time_label.get_text(" ", strip=True)) != "עלות זמן":
        raise _invalid_response()
    rate_blocks = [
        block for block in cells[2].select(":scope > div.rates") if isinstance(block, Tag)
    ]
    bands = _parse_bands(rate_blocks)

    return _TariffDetail(
        booking_id=expected_booking_id,
        tariff_name=tariff_name,
        distance_rate=distance_rate,
        bands=bands,
        car_key=_car_key(facts["רכב"]),
    )


def _request_datetimes(payload: Mapping[str, object]) -> Tuple[datetime, datetime]:
    start_at = payload.get("start_at")
    end_at = payload.get("end_at")
    if not isinstance(start_at, datetime) or not isinstance(end_at, datetime):
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="start_at and end_at must be timezone-aware datetimes",
        )
    if (
        start_at.tzinfo is None
        or start_at.utcoffset() is None
        or end_at.tzinfo is None
        or end_at.utcoffset() is None
        or start_at.second != 0
        or start_at.microsecond != 0
        or end_at.second != 0
        or end_at.microsecond != 0
    ):
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="Estimate datetimes must include a UTC offset and use whole minutes",
        )
    try:
        start_utc = start_at.astimezone(timezone.utc)
        end_utc = end_at.astimezone(timezone.utc)
        start_local = start_at.astimezone(_JERUSALEM)
        end_local = end_at.astimezone(_JERUSALEM)
    except (OverflowError, OSError, ValueError):
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="Estimate interval is outside the supported datetime range",
        ) from None
    if start_utc >= end_utc:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="end_at must be after start_at",
        )
    if end_local.date() == date.max and end_local.time() != time.min:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="Estimate interval is outside the supported datetime range",
        )
    covered_day_count = (end_local.date() - start_local.date()).days
    if end_local.time() != time.min:
        covered_day_count += 1
    if covered_day_count > _MAX_INTERVAL_DAYS:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="Estimate intervals may cover at most 366 Jerusalem calendar days",
        )
    return start_at, end_at


def _request_distance(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="distance_km must be a nonnegative finite number",
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="distance_km must be a nonnegative finite number",
        ) from None
    if not parsed.is_finite() or parsed < 0:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="distance_km must be a nonnegative finite number",
        )
    return parsed


def _source_booking_id(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="tariff_source_booking_id must be a positive integer",
        )
    return value


def _covered_local_dates(
    start_at: datetime, end_at: datetime, *, unavailable: bool = True
) -> List[int]:
    try:
        start_local = start_at.astimezone(_JERUSALEM)
        end_local = end_at.astimezone(_JERUSALEM)
    except (OverflowError, OSError, ValueError):
        if unavailable:
            raise _tariff_unavailable("The requested interval cannot be priced in Jerusalem time") from None
        raise _invalid_response() from None
    stop = end_local.date()
    if end_local.time() != time.min:
        stop += timedelta(days=1)
    days: List[int] = []
    current = start_local.date()
    while current < stop:
        days.append(current.weekday())
        current += timedelta(days=1)
    return days


def _covered_dates(start_at: datetime, end_at: datetime) -> List[date]:
    start_local = start_at.astimezone(_JERUSALEM)
    end_local = end_at.astimezone(_JERUSALEM)
    stop = end_local.date()
    if end_local.time() != time.min:
        stop += timedelta(days=1)
    result: List[date] = []
    current = start_local.date()
    while current < stop:
        result.append(current)
        current += timedelta(days=1)
    return result


def _boundary_utc(local_day: date, minute: int) -> datetime:
    if minute == 24 * 60:
        local_day += timedelta(days=1)
        minute = 0
    naive = datetime.combine(local_day, time(minute // 60, minute % 60))
    first = naive.replace(tzinfo=_JERUSALEM, fold=0)
    second = naive.replace(tzinfo=_JERUSALEM, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise _tariff_unavailable(
            "A tariff boundary is ambiguous during the requested daylight-saving transition"
        )
    utc_value = first.astimezone(timezone.utc)
    if utc_value.astimezone(_JERUSALEM).replace(tzinfo=None) != naive:
        raise _tariff_unavailable(
            "A tariff boundary does not exist during the requested daylight-saving transition"
        )
    return utc_value


def _time_cost(
    start_at: datetime,
    end_at: datetime,
    bands: Mapping[int, Tuple[_Band, ...]],
) -> Decimal:
    start_utc = start_at.astimezone(timezone.utc)
    end_utc = end_at.astimezone(timezone.utc)
    start_local = start_at.astimezone(_JERUSALEM)
    end_local = end_at.astimezone(_JERUSALEM)
    total = Decimal(0)
    for local_day in _covered_dates(start_at, end_at):
        segment_start = (
            start_local.hour * 60 + start_local.minute
            if local_day == start_local.date()
            else 0
        )
        segment_end = (
            end_local.hour * 60 + end_local.minute
            if local_day == end_local.date()
            else 24 * 60
        )
        daily = bands[local_day.weekday()]
        for band in daily:
            if band.end_minute <= segment_start or band.start_minute >= segment_end:
                continue
            band_start = (
                start_utc
                if band.start_minute < segment_start
                else _boundary_utc(local_day, band.start_minute)
            )
            band_end = (
                end_utc
                if band.end_minute > segment_end
                else _boundary_utc(local_day, band.end_minute)
            )
            if band_end <= band_start:
                raise _tariff_unavailable(
                    "Historical tariff bands cannot represent this daylight-saving day"
                )
            overlap_start = max(start_utc, band_start)
            overlap_end = min(end_utc, band_end)
            if overlap_start < overlap_end:
                seconds = int((overlap_end - overlap_start).total_seconds())
                total += band.hourly_rate * Decimal(seconds) / _SECONDS_PER_HOUR
    return total


async def _get(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> str:
    try:
        response = await client.get(path, params=params)
    except httpx.RequestError:
        raise APIException(
            status_code=502,
            code="pricing_upstream_unavailable",
            message="The vehicle billing service is unavailable",
        ) from None
    if not 200 <= response.status_code < 300:
        raise APIException(
            status_code=502,
            code="pricing_upstream_failure",
            message="The vehicle billing service rejected the request",
        )
    try:
        return response.text
    except UnicodeDecodeError:
        raise _invalid_response() from None


def _same_bands(left: Sequence[_Band], right: Sequence[_Band]) -> bool:
    return tuple(left) == tuple(right)


def _json_number(value: Decimal, *, request_error: bool = False) -> float:
    result = float(value)
    if not math.isfinite(result):
        if request_error:
            raise APIException(
                status_code=400,
                code="invalid_estimate_request",
                message="distance_km is outside the supported numeric range",
            )
        raise _tariff_unavailable("The calculated estimate is outside the supported numeric range")
    return result


async def estimate_cost(client: httpx.AsyncClient, payload: dict) -> dict:
    """Estimate a full trip charge from explicitly observed historical tariffs.

    History is fetched on every call. At most 24 owned charge details are read, including
    the primary source, so a missing weekday fails transparently rather than causing an
    unbounded account-history scan.
    """
    if not isinstance(payload, dict):
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="Estimate payload must be an object",
        )
    start_at, end_at = _request_datetimes(payload)
    if "distance_km" not in payload:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="distance_km is required",
        )
    distance_km = _request_distance(payload["distance_km"])
    requested_source_id = _source_booking_id(payload.get("tariff_source_booking_id"))

    history_markup = await _get(client, "/drives.php")
    entries = _history_entries(history_markup)
    by_id = {entry.booking_id: entry for entry in entries}
    if requested_source_id is not None and requested_source_id not in by_id:
        raise _tariff_unavailable(
            "The requested tariff source booking is not present in the authenticated account history"
        )

    detail_cache: Dict[int, _TariffDetail] = {}
    unusable: Dict[int, str] = {}
    lookups = 0

    async def load(entry: _HistoryEntry) -> Optional[_TariffDetail]:
        nonlocal lookups
        if entry.booking_id in detail_cache:
            return detail_cache[entry.booking_id]
        if entry.booking_id in unusable:
            return None
        if lookups >= _MAX_DETAIL_LOOKUPS:
            return None
        lookups += 1
        markup = await _get(
            client,
            "/display_cost_details.php",
            params={"ord_id": entry.booking_id},
        )
        try:
            parsed = _detail(markup, entry.booking_id)
        except _UnusableTariff as exc:
            unusable[entry.booking_id] = str(exc)
            return None
        detail_cache[entry.booking_id] = parsed
        return parsed

    source_entry: Optional[_HistoryEntry] = None
    source: Optional[_TariffDetail] = None
    if requested_source_id is not None:
        source_entry = by_id[requested_source_id]
        source = await load(source_entry)
        if source is None:
            reason = unusable.get(requested_source_id, "the lookup limit was reached")
            raise _tariff_unavailable(
                "The requested tariff source is unusable: " + reason
            )
    else:
        for candidate in entries:
            parsed = await load(candidate)
            if parsed is not None:
                source_entry = candidate
                source = parsed
                break
        if source is None or source_entry is None:
            raise _tariff_unavailable(
                "No usable named tariff was found in the most recent 24 owned charge details"
            )

    required_weekdays = set(_covered_local_dates(start_at, end_at))
    merged_bands: Dict[int, Tuple[_Band, ...]] = dict(source.bands)
    source_ids: List[int] = [source.booking_id]
    missing = required_weekdays - set(merged_bands)

    if missing:
        remaining = [entry for entry in entries if entry.booking_id != source.booking_id]
        while missing and remaining and lookups < _MAX_DETAIL_LOOKUPS:
            candidate = max(
                remaining,
                key=lambda entry: (
                    bool(entry.weekdays & missing)
                    and entry.car_key == source_entry.car_key,
                    bool(entry.weekdays & missing),
                    entry.car_key == source_entry.car_key,
                    entry.start_at.timestamp(),
                ),
            )
            remaining.remove(candidate)
            parsed = await load(candidate)
            if parsed is None:
                continue
            if (
                parsed.tariff_name != source.tariff_name
                or parsed.distance_rate != source.distance_rate
            ):
                continue

            for weekday, candidate_bands in parsed.bands.items():
                existing = merged_bands.get(weekday)
                if existing is not None and not _same_bands(existing, candidate_bands):
                    raise _tariff_unavailable(
                        "Matching historical tariff records contain conflicting weekday rates"
                    )
            contributed = False
            for weekday in tuple(missing):
                candidate_bands = parsed.bands.get(weekday)
                if candidate_bands is not None:
                    merged_bands[weekday] = candidate_bands
                    missing.remove(weekday)
                    contributed = True
            if contributed:
                source_ids.append(parsed.booking_id)

    if missing:
        names = ", ".join(str(day) for day in sorted(missing))
        raise _tariff_unavailable(
            "Historical tariff rates are missing for requested weekday indexes "
            + names
            + "; provide an owned tariff source or retry after matching billed trips exist "
            + "(at most 24 charge details are inspected)"
        )

    raw_time_cost = _time_cost(start_at, end_at, merged_bands)
    try:
        raw_distance_cost = source.distance_rate * distance_km
        time_cost = raw_time_cost.quantize(_CENT, rounding=ROUND_HALF_UP)
        distance_cost = raw_distance_cost.quantize(_CENT, rounding=ROUND_HALF_UP)
        estimated_total = (time_cost + distance_cost).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
    except DecimalException:
        raise APIException(
            status_code=400,
            code="invalid_estimate_request",
            message="distance_km is outside the supported numeric range",
        ) from None

    distance_number = _json_number(distance_km, request_error=True)
    return {
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "distance_km": distance_number,
        "currency": "ILS",
        "estimated_total": _json_number(estimated_total),
        "time_cost": _json_number(time_cost),
        "distance_cost": _json_number(distance_cost),
        "tariff_name": source.tariff_name,
        "source": "historical_tariff",
        "source_booking_ids": source_ids,
        "assumptions": [
            "This is a local estimate from named tariffs observed in owned historical charge details, not an upstream quote.",
            "Time uses actual elapsed overlap with explicit Asia/Jerusalem weekday bands; daylight-saving changes can change elapsed band hours.",
            "Distance uses the caller-supplied expected kilometres at the historical per-kilometre rate.",
            "The estimate assumes a full charge and excludes discounts, caps, credits, and shared-charge allocation.",
            "Components are rounded half-up to cents and then summed; upstream rounding may differ.",
            "The eventual assigned car or tariff may differ, and rates may have changed since the source bookings.",
        ],
    }
