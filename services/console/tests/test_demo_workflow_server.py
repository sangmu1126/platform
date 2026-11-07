"""Browser entry and HTTP write boundaries of the isolated demo."""

import json

import pytest
from fastapi.testclient import TestClient
from test_demo_workflow import ACTIONS

from demo_workflow.runtime import DemoWorkflow
from demo_workflow.server import DemoApp


@pytest.fixture
def app(tmp_path):
    return DemoApp(DemoWorkflow.create(tmp_path / "demo"), port=18131)


def enter(client, app):
    return client.get("/__demo/open/" + app.bootstrap, follow_redirects=False)


def test_session_one_time_entry_and_sova_layout_preserved(app):
    with TestClient(app, base_url=app.origin) as client:
        assert client.get("/__demo/status").status_code == 403
        assert enter(client, app).status_code == 303
        assert enter(client, app).status_code == 404
        assert client.get("/__demo/status").json()["synthetic"] is True
        page = client.get("/console/")
        assert page.status_code == 200
        assert 'id="demo-workflow"' in page.text and 'id="connect-form"' in page.text
        assert page.text.count('id="demo-workflow"') == 1
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert client.get("/v1/plans/real-plan").status_code == 403
        assert client.get("/__demo/workflow.js").status_code == 200


@pytest.mark.parametrize(
    "fault",
    [
        "origin",
        "host",
        "header",
        "fetch-site",
        "mime",
        "extra",
        "large",
        "duplicate",
        "bool-version",
    ],
)
def test_browser_mutation_boundaries(app, fault):
    headers = {"Origin": app.origin, "X-Sova-Demo": "1", "Content-Type": "application/json"}
    body = json.dumps({"action": "advance", "expected_version": 1})
    if fault == "origin":
        headers["Origin"] = "https://foreign.invalid"
    elif fault == "host":
        headers["Host"] = "foreign.invalid"
    elif fault == "header":
        headers.pop("X-Sova-Demo")
    elif fault == "fetch-site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif fault == "mime":
        headers["Content-Type"] = "text/plain"
    elif fault == "extra":
        body = json.dumps({"action": "advance", "expected_version": 1, "target": "production"})
    elif fault == "large":
        body = "x" * 513
    elif fault == "duplicate":
        body = '{"action":"advance","action":"execute","expected_version":1}'
    elif fault == "bool-version":
        body = '{"action":"advance","expected_version":true}'
    with TestClient(app, base_url=app.origin) as client:
        enter(client, app)
        result = client.post("/__demo/action", headers=headers, content=body)
    assert result.status_code in (403, 409, 413, 422)
    assert app.runtime.store.snapshot(app.runtime.plan_id)["state"] == "COLLECT"


def test_http_full_journey_and_standard_write_routes_denied(app):
    headers = {"Origin": app.origin, "X-Sova-Demo": "1", "Content-Type": "application/json"}
    with TestClient(app, base_url=app.origin) as client:
        enter(client, app)
        assert client.post(f"/v1/plans/{app.runtime.plan_id}/cutover", json={}).status_code == 405
        for action in ACTIONS:
            view = client.get("/__demo/status").json()
            response = client.post(
                "/__demo/action",
                headers=headers,
                json={"action": action, "expected_version": view["plan"]["version"]},
            )
            assert response.status_code == 200, response.text
        final = client.get("/__demo/status").json()
        assert final["plan"]["state"] == "DONE"
        assert final["synthetic_effects"] == 1
