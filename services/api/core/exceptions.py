from fastapi import Request
from fastapi.responses import JSONResponse

from services.api.core.logging import logger


# Converts unhandled exceptions into a standardized API error response.
async def global_exception_handler(
    request: Request,
    exc: Exception,
):
    request_id = getattr(
        request.state,
        "request_id",
        None,
    )

    logger.exception(
        "unhandled_exception",
        request_id=request_id,
        error=str(exc),
    )

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "message": "Internal server error",
                "request_id": request_id,
            },
        },
    )