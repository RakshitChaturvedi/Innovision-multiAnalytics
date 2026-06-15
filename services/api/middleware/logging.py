import time

from fastapi import Request

from services.api.core.logging import logger


# Logs request metadata and execution time for monitoring and debugging.
async def logging_middleware(
    request: Request,
    call_next,
):
    start_time = time.time()

    response = await call_next(request)

    duration_ms = round(
        (time.time() - start_time) * 1000,
        2
    )

    logger.info(
        "request_completed",
        request_id=getattr(
            request.state,
            "request_id",
            None,
        ),
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )

    return response