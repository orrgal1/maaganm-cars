from datetime import datetime
from typing import Annotated, Literal, Optional

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


def _require_minute_precision(value: datetime) -> datetime:
    if value.second != 0 or value.microsecond != 0:
        raise ValueError("datetime must be aligned to a whole minute")
    return value


MinuteDatetime = Annotated[AwareDatetime, AfterValidator(_require_minute_precision)]
AccountName = Literal["personal", "work"]
PreferenceValue = Literal[-1, 0, 1]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _member_id_field():
    # Excluded from dumps (driver payloads, idempotency hashes) and from repr.
    return Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Optional 4-digit Sidur member code, equivalent to the X-Sidur-Member "
            "header and taking precedence over account. Never logged or returned."
        ),
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = Field(description="Local API health status.")
    configured_accounts: list[AccountName] = Field(
        description="Accounts with upstream Cars codes configured locally on this service."
    )


class DestinationOption(BaseModel):
    id: int = Field(description="Destination identifier accepted by booking mutations.")
    label: str = Field(description="Destination label from the upstream booking form.")


class PurposeOption(BaseModel):
    id: int = Field(description="Purpose identifier accepted by booking mutations.")
    label: str = Field(description="Purpose label from the upstream booking form.")


class PreferenceChoice(BaseModel):
    value: PreferenceValue = Field(description="Preference value submitted upstream.")
    label: str = Field(description="Human-readable choice label from the booking form.")


class CarPreferenceOption(BaseModel):
    id: str = Field(description="Car-preference field identifier from the booking form.")
    label: str = Field(description="Human-readable car-preference label.")
    choices: list[PreferenceChoice] = Field(
        description="Allowed values and labels for this preference."
    )


class BookingOptionsResponse(BaseModel):
    destinations: list[DestinationOption] = Field(
        description="Currently available destination choices."
    )
    purposes: list[PurposeOption] = Field(
        description="Currently available trip-purpose choices."
    )
    car_preferences: list[CarPreferenceOption] = Field(
        description="Optional automatic-assignment preferences from the live form."
    )
    timezone: Literal["Asia/Jerusalem"] = Field(
        description="Timezone used by the upstream booking service."
    )
    assignment_mode: Literal["automatic"] = Field(
        description="Cars are assigned automatically; a preference is not a guarantee."
    )


class Booking(BaseModel):
    id: int = Field(description="Upstream booking identifier.")
    start_at: AwareDatetime = Field(description="Timezone-aware booking start time.")
    end_at: AwareDatetime = Field(description="Timezone-aware booking end time.")
    destination: str = Field(description="Destination label.")
    purpose: str = Field(description="Trip-purpose label.")
    comment: str = Field(description="Booker comment.")
    car_label: Optional[str] = Field(
        description="Assigned car label, or null while no car is assigned."
    )
    status: str = Field(description="Current upstream booking status.")


class BookingsResponse(BaseModel):
    bookings: list[Booking] = Field(description="Bookings owned by the selected account.")
    total: int = Field(ge=0, description="Number of returned bookings.")


class BookingResponse(BaseModel):
    booking: Booking = Field(description="Created or updated booking.")


class CancelBookingResponse(BaseModel):
    id: int = Field(description="Cancelled upstream booking identifier.")
    cancelled: Literal[True] = Field(description="Confirms that cancellation succeeded.")


class BookingCreateRequest(_RequestModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "example": {
                "start_at": "2026-10-12T08:00:00+03:00",
                "end_at": "2026-10-12T09:30:00+03:00",
                "destination_id": 1,
                "purpose_id": 1,
                "comment": "Morning appointment",
                "dispatcher_comment": "",
            }
        },
    )

    start_at: MinuteDatetime = Field(
        description="Timezone-aware start time aligned to a whole minute."
    )
    end_at: MinuteDatetime = Field(
        description="Timezone-aware end time aligned to a whole minute."
    )
    destination_id: int = Field(
        description="Destination ID obtained from GET /booking-options."
    )
    purpose_id: int = Field(
        description="Purpose ID obtained from GET /booking-options."
    )
    comment: str = Field(default="", description="Comment visible on the booking.")
    dispatcher_comment: str = Field(
        default="", description="Optional comment for the dispatcher."
    )
    car_preferences: dict[str, PreferenceValue] = Field(
        default_factory=dict,
        description=(
            "Automatic-assignment preferences keyed by IDs from GET /booking-options; "
            "allowed values are -1, 0, and 1."
        ),
    )
    member_id: Optional[str] = _member_id_field()

    @model_validator(mode="after")
    def validate_interval(self) -> "BookingCreateRequest":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")
        return self


class BookingUpdateRequest(_RequestModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "example": {
                "end_at": "2026-10-12T10:00:00+03:00",
                "comment": "Return after appointment",
            }
        },
    )

    start_at: Optional[MinuteDatetime] = Field(
        default=None,
        description="Replacement timezone-aware start time, aligned to a whole minute.",
    )
    end_at: Optional[MinuteDatetime] = Field(
        default=None,
        description="Replacement timezone-aware end time, aligned to a whole minute.",
    )
    destination_id: Optional[int] = Field(
        default=None, description="Replacement destination ID from GET /booking-options."
    )
    purpose_id: Optional[int] = Field(
        default=None, description="Replacement purpose ID from GET /booking-options."
    )
    comment: Optional[str] = Field(default=None, description="Replacement booking comment.")
    dispatcher_comment: Optional[str] = Field(
        default=None, description="Replacement dispatcher comment."
    )
    car_preferences: Optional[dict[str, PreferenceValue]] = Field(
        default=None,
        description="Replacement automatic-assignment preferences.",
    )
    member_id: Optional[str] = _member_id_field()

    @model_validator(mode="after")
    def validate_changes(self) -> "BookingUpdateRequest":
        supplied = self.model_fields_set - {"member_id"}
        if not supplied:
            raise ValueError("at least one booking field must be supplied")
        if any(getattr(self, field_name) is None for field_name in supplied):
            raise ValueError("booking fields cannot be null")
        if "start_at" in supplied and "end_at" in supplied:
            assert self.start_at is not None and self.end_at is not None
            if self.end_at <= self.start_at:
                raise ValueError("end_at must be later than start_at")
        return self


class AvailableCar(BaseModel):
    id: int = Field(description="Car identifier shown by the scheduling source.")
    label: str = Field(description="Car label shown by the scheduling source.")
    details: list[str] = Field(description="Additional non-sensitive scheduling details.")


class AvailableCarsResponse(BaseModel):
    start_at: AwareDatetime = Field(description="Requested timezone-aware interval start.")
    end_at: AwareDatetime = Field(description="Requested timezone-aware interval end.")
    cars: list[AvailableCar] = Field(
        description="Cars that appear available in the scheduling source."
    )
    total: int = Field(ge=0, description="Number of returned cars.")
    source: Literal["schedule"] = Field(description="Source used for the availability view.")
    assignment_guaranteed: Literal[False] = Field(
        description=(
            "Always false: scheduling availability does not guarantee that the "
            "automatic assignment process will assign a specific car."
        )
    )


class EstimateCostRequest(_RequestModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "example": {
                "start_at": "2026-10-12T08:00:00+03:00",
                "end_at": "2026-10-12T09:30:00+03:00",
                "distance_km": 24.5,
            }
        },
    )

    start_at: MinuteDatetime = Field(
        description="Timezone-aware trip start aligned to a whole minute."
    )
    end_at: MinuteDatetime = Field(
        description="Timezone-aware trip end aligned to a whole minute."
    )
    distance_km: float = Field(
        ge=0, description="Required nonnegative planned distance in kilometres."
    )
    tariff_source_booking_id: Optional[int] = Field(
        default=None,
        gt=0,
        description=(
            "Optional owned billed-trip ID whose historical tariff should be used. "
            "When omitted, the most recent usable billed trip is selected."
        ),
    )
    member_id: Optional[str] = _member_id_field()

    @model_validator(mode="after")
    def validate_interval(self) -> "EstimateCostRequest":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")
        return self


class EstimateCostResponse(BaseModel):
    start_at: AwareDatetime = Field(description="Estimated trip interval start.")
    end_at: AwareDatetime = Field(description="Estimated trip interval end.")
    distance_km: float = Field(ge=0, description="Distance used for the estimate.")
    currency: Literal["ILS"] = Field(description="Currency of all cost values.")
    estimated_total: float = Field(
        ge=0, description="Estimated time cost plus estimated distance cost."
    )
    time_cost: float = Field(ge=0, description="Historical-tariff time component.")
    distance_cost: float = Field(
        ge=0, description="Historical-tariff distance component."
    )
    tariff_name: str = Field(description="Historical tariff name used for the estimate.")
    source: Literal["historical_tariff"] = Field(
        description="Declares that this is a local historical-tariff estimate."
    )
    source_booking_ids: list[int] = Field(
        description="Owned billed trips used as evidence for the selected tariff."
    )
    assumptions: list[str] = Field(
        description="Important limitations and assumptions for interpreting the estimate."
    )


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Safe human-readable error message.")


class ErrorResponse(BaseModel):
    error: ErrorDetail
