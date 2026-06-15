from fastapi.testclient import TestClient

from services.api.main import app

client = TestClient(app)


# Verifies that the health endpoint returns the expected response.
def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200

    assert response.json() == {
        "status": "healthy",
        "service": "api",
    }