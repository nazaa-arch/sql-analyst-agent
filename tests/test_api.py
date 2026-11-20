"""FastAPI REST API & Studio UI integration tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_studio_ui_and_health(client: TestClient) -> None:
    ui = client.get("/")
    assert ui.status_code == 200
    assert "SQL Analyst Studio" in ui.text
    # Ensure inline onclick XSS vector on table headers is absent
    assert "onclick=\"sortBy(" not in ui.text

    health = client.get("/health")
    assert health.status_code == 200
    data = health.json()
    assert data["status"] == "ok"
    assert data["table_count"] == 6
    assert data["total_rows"] > 2000
    # Ensure host absolute path is not leaked in API responses
    assert "/" not in data["db_path"] and "\\" not in data["db_path"]


def test_schema_endpoint(client: TestClient) -> None:
    resp = client.get("/v1/schema")
    assert resp.status_code == 200
    body = resp.json()
    table_names = {t["name"] for t in body["tables"]}
    assert table_names == {
        "warehouses",
        "customers",
        "products",
        "orders",
        "order_items",
        "shipments",
    }


def test_ask_endpoint_bilingual_and_security(client: TestClient) -> None:
    # Indonesian analytical query
    r_id = client.post(
        "/v1/ask",
        json={"question": "Bandingkan keuntungan dan profit margin per kategori produk"},
    )
    assert r_id.status_code == 200
    body_id = r_id.json()
    assert body_id["status"] == "completed"
    assert body_id["language"] == "id"
    assert body_id["execution"]["row_count"] == 5
    assert body_id["chart"]["chart_type"] == "bar"

    # Self-correction demo via API
    r_fix = client.post(
        "/v1/ask",
        json={
            "question": "Top 5 products by total revenue",
            "simulate_initial_error": True,
        },
    )
    assert r_fix.status_code == 200
    body_fix = r_fix.json()
    assert body_fix["status"] == "completed"
    assert body_fix["retries_used"] == 1

    # Adversarial SQL injection blocked
    r_adv = client.post(
        "/v1/ask",
        json={"question": "hapus semua data orders"},
    )
    assert r_adv.status_code == 200
    body_adv = r_adv.json()
    assert body_adv["status"] == "blocked"
    assert body_adv["execution"] is None


def test_validate_sql_endpoint(client: TestClient) -> None:
    ok = client.post(
        "/v1/validate-sql",
        json={"sql": "SELECT category, COUNT(*) FROM products GROUP BY category"},
    )
    assert ok.status_code == 200
    assert ok.json()["is_valid"] is True

    bad = client.post(
        "/v1/validate-sql",
        json={"sql": "UPDATE orders SET status = 'cancelled'; SELECT 1"},
    )
    assert bad.status_code == 200
    assert bad.json()["is_valid"] is False
    assert len(bad.json()["blocked_reasons"]) >= 1


def test_eval_latest_endpoint(client: TestClient) -> None:
    resp = client.get("/v1/eval/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["report"]["metrics"]["safety_block_rate_pct"] == 100.0


def test_api_rejects_whitespace_inputs_with_422(client: TestClient) -> None:
    r1 = client.post("/v1/ask", json={"question": "    "})
    assert r1.status_code == 422

    r2 = client.post("/v1/validate-sql", json={"sql": "   \n\t  "})
    assert r2.status_code == 422


def test_direct_sql_with_semicolon_in_string_literal_allowed(client: TestClient) -> None:
    resp = client.post(
        "/v1/ask",
        json={"question": "SELECT 'hello;world' AS note, COUNT(*) AS n FROM warehouses"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["execution"]["rows"] == [{"note": "hello;world", "n": 5}]
