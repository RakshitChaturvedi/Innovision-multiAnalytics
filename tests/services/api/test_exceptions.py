from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.core.exceptions import (
    global_exception_handler,
)

app = FastAPI()

app.add_exception_handler(
    Exception,
    global_exception_handler,
)


@app.get("/boom")
async def boom():
    raise Exception("Test exception")


client = TestClient(
    app,
    raise_server_exceptions=False
)


# Verifies that unhandled exceptions return the standardized error format.
def test_global_exception_handler():
    response = client.get("/boom")

    assert response.status_code == 500

    body = response.json()

    assert body["success"] is False

    assert "error" in body

    assert body["error"]["message"] == (
        "Internal server error"
    )