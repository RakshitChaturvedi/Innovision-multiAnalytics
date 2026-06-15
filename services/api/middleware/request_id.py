import uuid

from fastapi import Request


# Generates a UUID for identification and tracking.
async def request_id_middleware(
    request: Request,
    call_next,
):
    request_id = str(uuid.uuid4())

    request.state.request_id = request_id

    response = await call_next(request)

    response.headers["X-Request-ID"] = request_id

    return response