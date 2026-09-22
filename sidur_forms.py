"""Pure HTML, form, booking, and date helpers for the Sidur integration."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Mapping, Optional, Sequence, TypedDict, Union
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, NavigableString, Tag


JERUSALEM = ZoneInfo("Asia/Jerusalem")
AuthState = Literal["authenticated", "login", "unknown"]
FormKind = Literal["create", "edit"]
Markup = Union[str, bytes, BeautifulSoup, Tag]
Scalar = Union[str, int, float]
ControlList = list[tuple[str, str]]


class FormOption(TypedDict):
    value: str
    label: str
    selected: bool
    disabled: bool


class PreferenceGroup(TypedDict):
    name: str
    label: str
    choices: list[FormOption]
    selected: Optional[str]


class BookingFormOptions(TypedDict):
    destinations: list[FormOption]
    purposes: list[FormOption]
    preferences: list[PreferenceGroup]
    selected_defaults: dict[str, str]


class Booking(TypedDict):
    id: int
    start_at: str
    end_at: str
    destination: str
    purpose: str
    comment: str
    car_label: Optional[str]
    status: str


class BookingFormState(TypedDict):
    kind: FormKind
    form_id: str
    action: str
    booking_id: Optional[int]
    ord: Optional[int]
    start_at: datetime
    end_at: datetime
    destination_id: str
    purpose_id: str
    comment: str
    dispatcher_comment: str
    car_preferences: dict[str, str]
    controls: ControlList
    options: BookingFormOptions


_SPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"\A\d{2}-\d{2}-\d{4}\Z")
_TIME_RE = re.compile(r"\A\d{2}:\d{2}\Z")
_API_DATETIME_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\Z"
)
_HIDDEN_STYLE_RE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.I)


def _document(markup: Markup) -> Union[BeautifulSoup, Tag]:
    if isinstance(markup, (BeautifulSoup, Tag)):
        return markup
    return BeautifulSoup(markup, "html.parser")


def _clean_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()


def authentication_state(markup: Markup) -> AuthState:
    """Classify a response without relying on page titles or localized copy."""
    document = _document(markup)
    if document.select_one("a[href*='logout'], form#regular_order, form#edit_order_form"):
        return "authenticated"
    if document.select_one(".orders_row, #pref_groups, #content_table"):
        return "authenticated"

    password = document.select_one("form input[type='password']")
    if password is not None:
        form = password.find_parent("form")
        action = str(form.get("action", "")).lower() if isinstance(form, Tag) else ""
        identifiers = (
            "login",
            "signin",
            "sign-in",
            "authentication",
            "password",
            "username",
            "user_name",
        )
        names = {
            str(control.get("name", "")).lower()
            for control in form.select("input[name]")
        } if isinstance(form, Tag) else set()
        if any(identifier in action for identifier in identifiers) or any(
            identifier in names for identifier in identifiers
        ):
            return "login"
        # A password-bearing anonymous form is safer to treat as a login than as
        # a valid authenticated Sidur response.
        return "login"
    return "unknown"


def is_login_page(markup: Markup) -> bool:
    """Return whether the response is an authentication form."""
    return authentication_state(markup) == "login"


def is_authenticated_page(markup: Markup) -> bool:
    """Return whether the response contains authenticated Sidur structure."""
    return authentication_state(markup) == "authenticated"


def _find_form(
    document: Union[BeautifulSoup, Tag], selector: Optional[str]
) -> Tag:
    if document.name == "form" and selector is None:
        return document
    if selector is not None:
        form = document.select_one(selector)
    else:
        form = document.select_one("form#regular_order, form#edit_order_form")
        if form is None:
            forms = document.find_all("form")
            form = forms[0] if len(forms) == 1 else None
    if not isinstance(form, Tag) or form.name != "form":
        target = selector or "a unique booking form"
        raise ValueError(f"Sidur response does not contain {target}")
    return form


def _source_form(markup: Markup, selector: Optional[str]) -> Tag:
    """Parse a form in isolation so malformed surrounding tables cannot close it."""
    if isinstance(markup, (str, bytes)):
        pattern = (
            rb"<form\b.*?</form\s*>"
            if isinstance(markup, bytes)
            else r"<form\b.*?</form\s*>"
        )
        candidates: list[Tag] = []
        for match in re.finditer(pattern, markup, re.I | re.S):
            fragment = BeautifulSoup(match.group(0), "html.parser")
            if selector is not None:
                form = fragment.select_one(selector)
                if isinstance(form, Tag) and form.name == "form":
                    return form
            else:
                candidates.extend(
                    form
                    for form in fragment.find_all("form")
                    if isinstance(form, Tag)
                )
        for form in candidates:
            if form.get("id") in {"regular_order", "edit_order_form"}:
                return form
        if selector is None and len(candidates) == 1:
            return candidates[0]
    return _find_form(_document(markup), selector)


def _hidden_by_attribute(tag: Tag) -> bool:
    classes = {str(value).lower() for value in tag.get("class", [])}
    return (
        tag.has_attr("hidden")
        or str(tag.get("aria-hidden", "")).lower() == "true"
        or "hidden" in classes
        or bool(_HIDDEN_STYLE_RE.search(str(tag.get("style", ""))))
    )


def _is_effectively_disabled(control: Tag) -> bool:
    if control.has_attr("disabled"):
        return True
    if control.name == "option":
        group = control.find_parent("optgroup")
        if isinstance(group, Tag) and group.has_attr("disabled"):
            return True
    for ancestor in control.parents:
        if not isinstance(ancestor, Tag) or ancestor.name != "fieldset":
            continue
        if not ancestor.has_attr("disabled"):
            continue
        first_legend = ancestor.find("legend", recursive=False)
        if isinstance(first_legend, Tag) and (
            control is first_legend or first_legend in control.parents
        ):
            continue
        return True
    return False


def _effective_selected_options(select: Tag) -> list[Tag]:
    options = [
        option for option in select.find_all("option") if isinstance(option, Tag)
    ]
    enabled = [option for option in options if not _is_effectively_disabled(option)]
    selected = [option for option in enabled if option.has_attr("selected")]
    if select.has_attr("multiple"):
        return selected
    if any(option.has_attr("selected") for option in options):
        return selected[-1:]
    return enabled[:1]


def _option_value(option: Tag) -> str:
    if option.has_attr("value"):
        return str(option["value"])
    return _clean_text(option.get_text(" ", strip=True))


def extract_successful_controls(
    markup: Markup,
    form_selector: Optional[str] = None,
    *,
    include_submit_order: bool = False,
) -> ControlList:
    """Extract successful form controls in DOM order.

    Duplicate names are preserved. Disabled and unnamed controls, unchecked
    radios/checkboxes, and button controls are omitted. ``submit_order`` is
    included only when explicitly requested, representing the clicked submit
    button. A single-select with no explicit selection contributes its first
    enabled option, matching browser form submission.
    """
    form = _source_form(markup, form_selector)
    controls: ControlList = []
    submit_added = False

    for control in form.find_all(("input", "select", "textarea", "button")):
        if not isinstance(control, Tag) or _is_effectively_disabled(control):
            continue
        name = control.get("name")
        if name is None or str(name) == "":
            continue
        name = str(name)

        if control.name == "button":
            control_type = str(control.get("type", "submit")).lower()
            if (
                include_submit_order
                and not submit_added
                and name == "submit_order"
                and control_type == "submit"
            ):
                controls.append((name, str(control.get("value", ""))))
                submit_added = True
            continue

        if control.name == "textarea":
            controls.append((name, control.get_text()))
            continue

        if control.name == "select":
            for option in _effective_selected_options(control):
                controls.append((name, _option_value(option)))
            continue

        control_type = str(control.get("type", "text")).lower()
        if control_type in {"checkbox", "radio"}:
            if control.has_attr("checked"):
                controls.append((name, str(control.get("value", "on"))))
            continue
        if control_type == "submit":
            if include_submit_order and not submit_added and name == "submit_order":
                controls.append((name, str(control.get("value", ""))))
                submit_added = True
            continue
        if control_type in {"button", "reset", "image", "file"}:
            continue
        controls.append((name, str(control.get("value", ""))))

    return controls


def overlay_scalar_controls(
    controls: Sequence[tuple[str, str]], values: Mapping[str, Scalar]
) -> ControlList:
    """Overlay scalar values while preserving all untouched controls and order.

    The first occurrence of an overlaid name is replaced and any further
    occurrences of that *touched* name are removed. Missing names are appended
    in mapping order. Duplicate names not present in ``values`` remain intact.
    """
    replacements = {name: str(value) for name, value in values.items()}
    emitted: set[str] = set()
    result: ControlList = []
    for name, value in controls:
        if name not in replacements:
            result.append((name, value))
        elif name not in emitted:
            result.append((name, replacements[name]))
            emitted.add(name)
    for name, value in replacements.items():
        if name not in emitted:
            result.append((name, value))
    return result


def _select_options(select: Optional[Tag]) -> list[FormOption]:
    if not isinstance(select, Tag):
        return []
    selected = {id(option) for option in _effective_selected_options(select)}
    return [
        FormOption(
            value=_option_value(option),
            label=_clean_text(option.get_text(" ", strip=True)),
            selected=id(option) in selected,
            disabled=_is_effectively_disabled(option),
        )
        for option in select.find_all("option")
        if isinstance(option, Tag)
    ]


def extract_form_options(
    markup: Markup, form_selector: Optional[str] = None
) -> BookingFormOptions:
    """Extract dynamic destination, purpose, and car-preference choices."""
    form = _source_form(markup, form_selector)
    destinations = _select_options(form.select_one("select#dest, select[name='dest']"))
    purposes = _select_options(
        form.select_one("select#drive_purpose, select[name='drive_purpose']")
    )
    selected_defaults: dict[str, str] = {}
    for name, options in (("dest", destinations), ("drive_purpose", purposes)):
        selected = next((option["value"] for option in options if option["selected"]), None)
        if selected is not None:
            selected_defaults[name] = selected

    preferences: list[PreferenceGroup] = []
    table = form.select_one("#pref_groups")
    if isinstance(table, Tag):
        title_row = table.select_one("tr.titles")
        header_labels = (
            [_clean_text(item.get_text(" ", strip=True)) for item in title_row.find_all("div")]
            if isinstance(title_row, Tag)
            else []
        )
        for row in table.find_all("tr"):
            if not isinstance(row, Tag):
                continue
            label_cell = row.select_one(".grp-name")
            radios = row.select("input[type='radio'][name^='grp_val_']")
            if not isinstance(label_cell, Tag) or not radios:
                continue
            name = str(radios[0].get("name", ""))
            if not name:
                continue
            choices: list[FormOption] = []
            selected_value: Optional[str] = None
            for index, radio in enumerate(radios):
                value = str(radio.get("value", "on"))
                explicit_label = None
                radio_id = radio.get("id")
                if radio_id:
                    explicit_label = row.find("label", attrs={"for": str(radio_id)})
                choice_label = (
                    _clean_text(explicit_label.get_text(" ", strip=True))
                    if isinstance(explicit_label, Tag)
                    else ""
                )
                if not choice_label:
                    choice_label = header_labels[index] if index < len(header_labels) else value
                is_selected = radio.has_attr("checked") and not _is_effectively_disabled(radio)
                if is_selected and selected_value is None:
                    selected_value = value
                choices.append(
                    FormOption(
                        value=value,
                        label=choice_label,
                        selected=is_selected,
                        disabled=_is_effectively_disabled(radio),
                    )
                )
            if selected_value is not None:
                selected_defaults[name] = selected_value
            preferences.append(
                PreferenceGroup(
                    name=name,
                    label=_clean_text(label_cell.get_text(" ", strip=True)),
                    choices=choices,
                    selected=selected_value,
                )
            )

    return BookingFormOptions(
        destinations=destinations,
        purposes=purposes,
        preferences=preferences,
        selected_defaults=selected_defaults,
    )


def _localize_wall_time(value: datetime) -> datetime:
    if value.tzinfo is not None:
        raise ValueError("local Sidur datetime must not include a timezone")
    valid: list[datetime] = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=JERUSALEM, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(JERUSALEM)
        if round_trip.replace(tzinfo=None) == value and round_trip.fold == fold:
            valid.append(candidate)
    if not valid:
        raise ValueError("local Sidur datetime does not exist in Asia/Jerusalem")
    if len(valid) > 1 and valid[0].utcoffset() != valid[1].utcoffset():
        raise ValueError("local Sidur datetime is ambiguous in Asia/Jerusalem")
    return valid[0]


def parse_local_datetime(date_text: str, time_text: str) -> datetime:
    """Parse strict ``DD-MM-YYYY`` and ``HH:MM`` Sidur fields in Jerusalem."""
    if not _DATE_RE.fullmatch(date_text) or not _TIME_RE.fullmatch(time_text):
        raise ValueError("Sidur datetime must use DD-MM-YYYY and HH:MM without seconds")
    try:
        naive = datetime.strptime(f"{date_text} {time_text}", "%d-%m-%Y %H:%M")
    except ValueError as exc:
        raise ValueError("invalid Sidur local date or time") from exc
    return _localize_wall_time(naive)


def parse_api_datetime(value: Union[datetime, str]) -> datetime:
    """Normalize an aware minute-precision API datetime to Asia/Jerusalem."""
    if isinstance(value, str):
        text = value.strip()
        if not _API_DATETIME_RE.fullmatch(text):
            raise ValueError(
                "API datetime must be aware ISO YYYY-MM-DDTHH:MM±HH:MM without seconds"
            )
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid API datetime") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("API datetime must be a datetime or ISO string")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("API datetime must include an explicit timezone")
    if parsed.second or parsed.microsecond:
        raise ValueError("API datetime must not include seconds or microseconds")
    local = parsed.astimezone(JERUSALEM)
    # Reject wall times that cannot be represented uniquely by Sidur's timezone-
    # free form fields, even when the supplied instant itself is unambiguous.
    _localize_wall_time(local.replace(tzinfo=None))
    return local


def api_datetime_to_form(
    value: Union[datetime, str]
) -> tuple[str, str, datetime]:
    """Return Sidur date/time fields plus the normalized aware instant."""
    local = parse_api_datetime(value)
    return local.strftime("%d-%m-%Y"), local.strftime("%H:%M"), local


def format_form_datetime(value: Union[datetime, str]) -> str:
    """Format an API datetime as Sidur's ``DD-MM-YYYY HH:MM`` representation."""
    date_text, time_text, _ = api_datetime_to_form(value)
    return f"{date_text} {time_text}"


def validate_datetime_range(
    start: Union[datetime, str], end: Union[datetime, str]
) -> tuple[datetime, datetime]:
    """Normalize a range and require its end instant to be after its start."""
    normalized_start = parse_api_datetime(start)
    normalized_end = parse_api_datetime(end)
    if normalized_end.astimezone(timezone.utc) <= normalized_start.astimezone(
        timezone.utc
    ):
        raise ValueError("end datetime must be after start datetime")
    return normalized_start, normalized_end


def combine_form_datetimes(
    start_date: str,
    start_time: str,
    end_date: str,
    end_time: str,
    *,
    ensure_end_after_start: bool = True,
) -> tuple[datetime, datetime]:
    """Parse a pair of local form datetimes and optionally validate ordering."""
    start = parse_local_datetime(start_date, start_time)
    end = parse_local_datetime(end_date, end_time)
    if ensure_end_after_start and end.astimezone(
        timezone.utc
    ) <= start.astimezone(timezone.utc):
        raise ValueError("end datetime must be after start datetime")
    return start, end


def extract_booking_form(
    markup: Markup, form_selector: Optional[str] = None
) -> BookingFormState:
    """Extract current values and dynamic choices from a fresh create/edit form."""
    form = _source_form(markup, form_selector)
    controls = extract_successful_controls(form)
    values: dict[str, str] = {}
    for name, value in controls:
        values[name] = value

    required = ("start_date", "start_time", "end_date", "end_time", "dest", "drive_purpose")
    missing = [name for name in required if name not in values]
    if missing:
        raise ValueError(f"Sidur booking form is missing controls: {', '.join(missing)}")
    start, end = combine_form_datetimes(
        values["start_date"],
        values["start_time"],
        values["end_date"],
        values["end_time"],
    )

    form_id = str(form.get("id", ""))
    mode = values.get("mode", "")
    kind: FormKind = "edit" if form_id == "edit_order_form" or mode == "update" else "create"
    ord_text = values.get("ord")
    try:
        booking_id = int(ord_text) if ord_text not in (None, "") else None
    except ValueError as exc:
        raise ValueError("Sidur edit form contains a non-numeric booking id") from exc
    if kind == "edit" and booking_id is None:
        raise ValueError("Sidur edit form is missing its booking id")

    options = extract_form_options(form)
    car_preferences = {
        group["name"]: group["selected"]
        for group in options["preferences"]
        if group["selected"] is not None
    }
    return BookingFormState(
        kind=kind,
        form_id=form_id,
        action=str(form.get("action", "")),
        booking_id=booking_id,
        ord=booking_id,
        start_at=start,
        end_at=end,
        destination_id=values["dest"],
        purpose_id=values["drive_purpose"],
        comment=values.get("comment", ""),
        dispatcher_comment=values.get("comment_to_sadran", ""),
        car_preferences=car_preferences,
        controls=controls,
        options=options,
    )


def _text_without_classes(tag: Tag, excluded: set[str]) -> str:
    parts: list[str] = []
    for node in tag.find_all(string=True):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        omit = False
        while isinstance(parent, Tag):
            if excluded.intersection(str(value) for value in parent.get("class", [])):
                omit = True
                break
            if parent is tag:
                break
            parent = parent.parent
        if not omit:
            parts.append(str(node))
    return _clean_text(" ".join(parts))


def _epoch_iso(element: Tag, field: str) -> str:
    text = _clean_text(element.get_text(" ", strip=True))
    if not text.isdigit():
        raise ValueError(f"Sidur booking contains invalid {field} epoch seconds")
    try:
        return datetime.fromtimestamp(int(text), timezone.utc).astimezone(JERUSALEM).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"Sidur booking contains out-of-range {field} epoch seconds") from exc


def parse_bookings(markup: Markup) -> list[Booking]:
    """Parse Sidur ``.orders_row`` entries into the public booking shape."""
    document = _document(markup)
    list_table = document.select_one("#content_table.my_orders_non_sorted")
    if not isinstance(list_table, Tag) or list_table.select_one(".orders_head") is None:
        raise ValueError("Sidur response is not the owned-bookings list")
    bookings: list[Booking] = []
    for row in document.select(".orders_row"):
        id_element = row.select_one(".order_id")
        if not isinstance(id_element, Tag):
            raise ValueError("Sidur booking row is missing its id")
        id_text = _clean_text(id_element.get_text(" ", strip=True))
        try:
            booking_id = int(id_text)
        except ValueError as exc:
            raise ValueError("Sidur booking row contains a non-numeric id") from exc

        epochs = row.select(".times .sort_by_me")
        if len(epochs) < 2:
            raise ValueError(f"Sidur booking {booking_id} is missing start or end time")
        destination_element = row.select_one("#purpose")
        purpose_element = row.select_one("#dest")
        comment_element = row.select_one("#comment")
        car_element = row.select_one("#car")
        state_element = row.select_one("#order_state")
        if not isinstance(destination_element, Tag) or not isinstance(purpose_element, Tag):
            raise ValueError(f"Sidur booking {booking_id} is missing destination or purpose")
        if not isinstance(state_element, Tag):
            raise ValueError(f"Sidur booking {booking_id} is missing status")

        status = _text_without_classes(state_element, {"label"})
        if not status:
            titled = state_element if state_element.has_attr("title") else state_element.select_one("[title]")
            status = _clean_text(str(titled.get("title", ""))) if isinstance(titled, Tag) else ""
        if not status:
            raise ValueError(f"Sidur booking {booking_id} has an empty status")

        car_label = (
            _text_without_classes(car_element, {"label"})
            if isinstance(car_element, Tag)
            else ""
        )
        bookings.append(
            Booking(
                id=booking_id,
                start_at=_epoch_iso(epochs[0], "start"),
                end_at=_epoch_iso(epochs[1], "end"),
                destination=_text_without_classes(destination_element, {"label"}),
                purpose=_text_without_classes(purpose_element, {"label"}),
                comment=(
                    _text_without_classes(comment_element, {"label"})
                    if isinstance(comment_element, Tag)
                    else ""
                ),
                car_label=car_label or None,
                status=status,
            )
        )
    return bookings


def _visible_text(element: Tag) -> str:
    for ancestor in (element, *element.parents):
        if not isinstance(ancestor, Tag):
            continue
        if (
            ancestor.name in {"script", "style", "template", "noscript"}
            or _hidden_by_attribute(ancestor)
        ):
            return ""

    parts: list[str] = []
    for node in element.find_all(string=True):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        visible = True
        while isinstance(parent, Tag):
            if (
                parent.name in {"script", "style", "template", "noscript"}
                or _hidden_by_attribute(parent)
            ):
                visible = False
                break
            if parent is element:
                break
            parent = parent.parent
        if visible:
            parts.append(str(node))
    return _clean_text(" ".join(parts))


def extract_visible_errors(
    markup: Markup, *, max_messages: int = 5, max_length: int = 300
) -> list[str]:
    """Return concise text from explicit, visible upstream error elements only.

    There is deliberately no page-body or raw-HTML fallback: an unrecognized
    response yields an empty list rather than leaking upstream content.
    """
    if max_messages < 1 or max_length < 1:
        return []
    document = _document(markup)
    selector = (
        "#messages-inner .error, #messages .error, form .error, .alert-danger, "
        ".form-error, .validation-error, .error-message, #error, #errors"
    )
    messages: list[str] = []
    seen: set[str] = set()
    for element in document.select(selector):
        if not isinstance(element, Tag):
            continue
        text = _visible_text(element)
        if not text or text in seen:
            continue
        if len(text) > max_length:
            text = text[: max_length - 1].rstrip() + "…"
        seen.add(text)
        messages.append(text)
        if len(messages) >= max_messages:
            break
    return messages


__all__ = [
    "AuthState",
    "Booking",
    "BookingFormOptions",
    "BookingFormState",
    "ControlList",
    "FormOption",
    "FormKind",
    "JERUSALEM",
    "PreferenceGroup",
    "api_datetime_to_form",
    "authentication_state",
    "combine_form_datetimes",
    "extract_booking_form",
    "extract_form_options",
    "extract_successful_controls",
    "extract_visible_errors",
    "format_form_datetime",
    "is_authenticated_page",
    "is_login_page",
    "overlay_scalar_controls",
    "parse_api_datetime",
    "parse_bookings",
    "parse_local_datetime",
    "validate_datetime_range",
]
