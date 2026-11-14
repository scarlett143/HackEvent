"""API error shape from doc 06 Part A: `{"error": {"code", "message", "details"}}`."""

from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers


def error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        status_code=exc.status,
        headers=exc.headers,
    )


async def handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422 in the doc 06 shape; `details.errors` lists field + reason only."""
    assert isinstance(exc, RequestValidationError)
    errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return error_response(
        ApiError(422, "invalid_request", "Invalid request.", {"errors": jsonable_encoder(errors)})
    )
