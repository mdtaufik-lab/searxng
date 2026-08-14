from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

_GOOGLE_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        reason: str = "badRequest",
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason
        self.headers = headers or {}


def _google_body(err: GatewayError) -> dict:
    return {
        "error": {
            "code": err.status,
            "message": err.message,
            "errors": [
                {"message": err.message, "domain": "global", "reason": err.reason}
            ],
            "status": _GOOGLE_STATUS.get(err.status, "UNKNOWN"),
        }
    }


async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    # Google clients parse the google-shaped error envelope; giving them our own
    # shape on the compat endpoint would break their error handling as surely as
    # a wrong success shape would.
    if request.url.path.startswith("/customsearch"):
        body = _google_body(exc)
    else:
        body = {"error": {"code": exc.status, "reason": exc.reason, "message": exc.message}}
    return JSONResponse(status_code=exc.status, content=body, headers=exc.headers)
