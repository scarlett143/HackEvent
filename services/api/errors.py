"""API error shape from doc 06 Part A: `{"error": {"code", "message", "details"}}`."""

from typing import Any

from fastapi import Request
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


async def handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        status_code=exc.status,
        headers=exc.headers,
    )
