import uuid

from fastapi.testclient import TestClient

from services.api.main import app

client = TestClient(app)


# Verifies that every response contains a request ID header.
def test_request_id_header_exists():
    response = client.get("/health")

    assert "X-Request-ID" in response.headers


# Verifies that the request ID is a valid UUID.
def test_request_id_is_valid_uuid():
    response = client.get("/health")

    request_id = response.headers["X-Request-ID"]

    parsed_uuid = uuid.UUID(request_id)

    assert str(parsed_uuid) == request_id