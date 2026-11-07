import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from onprem_demo_server import DemoHost


class Manager:
    def __init__(self):
        self.actions = []

    def status(self):
        return {"phase": "idle", "session_id": None, "plan_id": None, "plan_digest": None}

    def act(self, body):
        self.actions.append(body)
        return {"accepted": True}


def setup():
    manager = Manager()
    app = DemoHost(manager, port=18142)
    client = TestClient(app, base_url="http://127.0.0.1:18142")
    assert (
        client.get("/__open/" + next(iter(app.tokens)), follow_redirects=False).status_code == 303
    )
    return app, manager, client


def test_demo_routes_require_private_session_and_leave_product_writes_closed():
    app, manager, client = setup()
    assert (
        TestClient(app, base_url="http://127.0.0.1:18142").get("/__demo/status").status_code == 403
    )
    status = client.get("/__demo/status")
    assert status.status_code == 200
    headers = {"Origin": "http://127.0.0.1:18142", "X-Demo-CSRF": status.json()["csrf"]}
    assert (
        client.post("/__demo/action", headers=headers, json={"unit": "accepted"}).status_code == 202
    )
    assert len(manager.actions) == 1
    assert client.post("/v1/plans/unit/approve", headers=headers, json={}).status_code == 403


@pytest.mark.parametrize("change", ["origin", "csrf", "cross-site", "type", "transfer"])
def test_demo_mutation_rejects_untrusted_browser_context(change):
    app, manager, client = setup()
    token = client.get("/__demo/status").json()["csrf"]
    headers = {
        "Origin": "http://127.0.0.1:18142",
        "X-Demo-CSRF": token,
        "Content-Type": "application/json",
    }
    if change == "origin":
        headers["Origin"] = "https://foreign.invalid"
    if change == "csrf":
        headers["X-Demo-CSRF"] = "invalid"
    if change == "cross-site":
        headers["Sec-Fetch-Site"] = "cross-site"
    if change == "type":
        headers["Content-Type"] = "text/plain"
    if change == "transfer":
        headers["Transfer-Encoding"] = "chunked"
    assert client.post("/__demo/action", headers=headers, content="{}").status_code == 403
    assert not manager.actions


def test_provider_error_does_not_expose_private_diagnostics():
    app, manager, client = setup()

    def fail(_):
        raise RuntimeError("private credential must not escape")

    manager.act = fail
    token = client.get("/__demo/status").json()["csrf"]
    response = client.post(
        "/__demo/action",
        headers={"Origin": "http://127.0.0.1:18142", "X-Demo-CSRF": token},
        json={},
    )
    assert response.status_code == 503
    assert "credential" not in response.text
    assert response.json()["retry_automatically"] is False
