import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import config
from cars_driver import CarsDriver
from idempotency import idempotency_scope, idempotency_store
from schemas import (
    AvailableCarsResponse,
    BookingCreateRequest,
    BookingOptionsResponse,
    BookingResponse,
    BookingsResponse,
    BookingUpdateRequest,
    CancelBookingResponse,
    ErrorResponse,
    EstimateCostRequest,
    EstimateCostResponse,
    HealthResponse,
    MinuteDatetime,
)
from security import APIException, CarCredentials, get_car_credentials, verify_bearer_token


logger = logging.getLogger("maaganm_cars.api")
cars_driver = CarsDriver()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        await cars_driver.close()


_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid request input."},
    401: {"model": ErrorResponse, "description": "Authentication or account credentials failed."},
    404: {"model": ErrorResponse, "description": "The requested booking was not found."},
    409: {"model": ErrorResponse, "description": "Request conflict or historical tariff unavailable."},
    502: {"model": ErrorResponse, "description": "The upstream booking service failed safely."},
    503: {"model": ErrorResponse, "description": "The selected account has no local Cars code configured."},
}

app = FastAPI(
    title="Maagan Michael Cars API",
    summary="Authenticated car-booking operations for automated agents.",
    description=(
        "A local authenticated API for reading and managing Maagan Michael car "
        "bookings. Booking assignment is automatic: schedule availability and car "
        "preferences never guarantee assignment of a particular car. Report bugs, "
        "integration feedback, and discussion at the GitHub issue tracker; do not "
        "include credentials or personal booking data."
    ),
    contact={
        "name": "GitHub issues",
        "url": "https://github.com/orrgal1/maaganm-cars/issues",
    },
    version="1.0.0",
    lifespan=lifespan,
    dependencies=[Depends(verify_bearer_token)],
)
_default_openapi = app.openapi


def _openapi_with_normalized_validation_errors() -> dict[str, Any]:
    schema = _default_openapi()
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.get("responses", {}).pop("422", None)
    return schema


app.openapi = _openapi_with_normalized_validation_errors



@app.exception_handler(APIException)
async def api_exception_handler(_: Request, exc: APIException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _: Request, __: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "code": "invalid_input",
                "message": (
                    "Request validation failed. Check required fields, value types, "
                    "timezone offsets, interval ordering, and whole-minute precision."
                ),
            }
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled %s while serving %s",
        type(exc).__name__,
        request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "error": {
                "code": "upstream_error",
                "message": "The upstream booking service request failed.",
            }
        },
    )


Credentials = Annotated[CarCredentials, Depends(get_car_credentials)]
IdempotencyKey = Annotated[
    Optional[str],
    Header(
        alias="Idempotency-Key",
        max_length=200,
        description=(
            "Optional process-local mutation key retained for up to 24 hours. Reusing "
            "it with an identical scoped request returns the original outcome without "
            "resubmitting. The in-memory key store is single-worker only and resets "
            "when the service restarts."
        ),
    ),
]


def _normalized_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise APIException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_input",
            message="Idempotency-Key cannot be empty or whitespace.",
        )
    return normalized


def _mutation_scope(credentials: CarCredentials, operation: str) -> str:
    return idempotency_scope(credentials, operation)


@app.get(
    "/health",
    response_model=HealthResponse,
    operation_id="getHealth",
    summary="Check authenticated API health",
    description=(
        "Confirms that the local service is running and lists accounts with local "
        "Cars codes configured. It does not contact or mutate the upstream service "
        "and never reveals car codes."
    ),
    responses={401: _ERROR_RESPONSES[401]},
    tags=["service"],
)
async def get_health() -> HealthResponse:
    configured_accounts = []
    if config.CARS_PERSONAL_CODE.strip():
        configured_accounts.append("personal")
    if config.CARS_WORK_CODE.strip():
        configured_accounts.append("work")
    return HealthResponse(status="ok", configured_accounts=configured_accounts)


@app.get(
    "/booking-options",
    response_model=BookingOptionsResponse,
    operation_id="getBookingOptions",
    summary="Read current booking form options",
    description=(
        "Fetches live destination, purpose, and automatic car-preference choices. "
        "Use these IDs in create and update requests instead of guessing them."
    ),
    responses=_ERROR_RESPONSES,
    tags=["bookings"],
)
async def get_booking_options(credentials: Credentials) -> dict[str, Any]:
    return await cars_driver.get_options(credentials)


@app.get(
    "/bookings",
    response_model=BookingsResponse,
    operation_id="listBookings",
    summary="List bookings owned by the selected account",
    description="Returns the selected account's bookings from the upstream service.",
    responses=_ERROR_RESPONSES,
    tags=["bookings"],
)
async def list_bookings(credentials: Credentials) -> dict[str, Any]:
    return await cars_driver.list_bookings(credentials)


@app.post(
    "/bookings",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    operation_id="createBooking",
    summary="Create a car booking",
    description=(
        "Submits one booking for automatic car assignment. Supply an Idempotency-Key "
        "when retrying is possible. Within one running API worker, the same key, "
        "account, credentials, operation, and payload are submitted at most once and "
        "retained for up to 24 hours. The in-memory protection resets on restart and "
        "is not shared across workers. A preference does not guarantee assignment of "
        "a specific car."
    ),
    responses=_ERROR_RESPONSES,
    tags=["bookings"],
)
async def create_booking(
    body: BookingCreateRequest,
    credentials: Credentials,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    driver_payload = body.model_dump()
    hash_payload = body.model_dump(mode="json")

    async def submit() -> dict[str, Any]:
        return await cars_driver.create_booking(credentials, driver_payload)

    return await idempotency_store.execute(
        key=_normalized_key(idempotency_key),
        scope=_mutation_scope(credentials, "POST /bookings"),
        payload=hash_payload,
        operation=submit,
    )


@app.patch(
    "/bookings/{booking_id}",
    response_model=BookingResponse,
    operation_id="updateBooking",
    summary="Update an owned booking",
    description=(
        "Updates only supplied fields on an existing booking. Null clears and empty "
        "updates are rejected. When only one interval boundary is supplied, the final "
        "interval is validated against the stored booking. Use an Idempotency-Key for "
        "safe retries."
    ),
    responses=_ERROR_RESPONSES,
    tags=["bookings"],
)
async def update_booking(
    booking_id: int,
    body: BookingUpdateRequest,
    credentials: Credentials,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    driver_changes = body.model_dump(exclude_unset=True)
    hash_payload = body.model_dump(mode="json", exclude_unset=True)

    async def submit() -> dict[str, Any]:
        return await cars_driver.update_booking(
            credentials, booking_id, driver_changes
        )

    return await idempotency_store.execute(
        key=_normalized_key(idempotency_key),
        scope=_mutation_scope(credentials, f"PATCH /bookings/{booking_id}"),
        payload=hash_payload,
        operation=submit,
    )


@app.delete(
    "/bookings/{booking_id}",
    response_model=CancelBookingResponse,
    operation_id="cancelBooking",
    summary="Cancel an owned booking",
    description=(
        "Cancels an existing booking owned by the selected account. Supply an "
        "Idempotency-Key so an uncertain response can be retrieved without issuing a "
        "second cancellation request."
    ),
    responses=_ERROR_RESPONSES,
    tags=["bookings"],
)
async def cancel_booking(
    booking_id: int,
    credentials: Credentials,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    async def submit() -> dict[str, Any]:
        return await cars_driver.cancel_booking(credentials, booking_id)

    return await idempotency_store.execute(
        key=_normalized_key(idempotency_key),
        scope=_mutation_scope(credentials, f"DELETE /bookings/{booking_id}"),
        payload={},
        operation=submit,
    )


@app.get(
    "/cars/available",
    response_model=AvailableCarsResponse,
    operation_id="getAvailableCars",
    summary="Inspect schedule-based car availability",
    description=(
        "Returns cars that appear available in the scheduling source for the requested "
        "interval. This is advisory only: availability does not reserve a car and does "
        "not guarantee the automatic assignment result."
    ),
    responses=_ERROR_RESPONSES,
    tags=["availability"],
)
async def get_available_cars(
    credentials: Credentials,
    start_at: Annotated[
        MinuteDatetime,
        Query(description="Timezone-aware interval start at whole-minute precision."),
    ],
    end_at: Annotated[
        MinuteDatetime,
        Query(description="Timezone-aware interval end at whole-minute precision."),
    ],
) -> dict[str, Any]:
    if end_at <= start_at:
        raise APIException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_input",
            message="end_at must be later than start_at.",
        )
    return await cars_driver.available_cars(credentials, start_at, end_at)


@app.post(
    "/bookings/estimate",
    response_model=EstimateCostResponse,
    operation_id="estimateBookingCost",
    summary="Estimate cost from an owned historical tariff",
    description=(
        "Calculates a local estimate from a named historical tariff parsed from the "
        "selected account's billed trips. Planned mileage is required. This is not an "
        "upstream quote: historical rates may differ from current rates or the tariff "
        "for the car eventually assigned. No booking mutation is performed."
    ),
    responses=_ERROR_RESPONSES,
    tags=["bookings"],
)
async def estimate_booking_cost(
    body: EstimateCostRequest,
    credentials: Credentials,
) -> dict[str, Any]:
    return await cars_driver.estimate_cost(credentials, body.model_dump())
