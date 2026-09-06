from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_product_writes_require_authentication() -> None:
    response = client.post("/api/products", json={"sku": "TEST-1", "name": "Test", "price": 100})
    assert response.status_code == 401


def test_product_routes_are_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/products" in paths
    assert "/api/products/{product_id}" in paths


def test_catalogue_read_routes_are_public() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/categories" in paths
    assert "security" not in paths["/api/products"] .get("get", {})
