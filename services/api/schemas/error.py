from pydantic import BaseModel


class ErrorDetail(BaseModel):
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail