"""Qualification-reader tests, never live acceptance results."""

import hashlib
import hmac
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

MODULE_PATH = Path(__file__).with_name("onprem_response_console.py")
spec = importlib.util.spec_from_file_location("onprem_reader", MODULE_PATH)
reader_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader_module)
REVISION = "c" * 40


def setup(root):
    root.chmod(0o700)
    at = datetime.now(UTC) - timedelta(seconds=1)
    binding = {
        "plan_id": "trial-plan",
        "plan_digest": "a" * 64,
        "plan_version": 1,
        "tenant_id": "onprem-qualification",
        "application_id": "trial-app",
        "source_environment_id": "trial-blue",
        "target_environment_id": "trial-green",
    }
    body = {
        "kind": "console-workload.v1",
        "authorizes_execution": False,
        "plan_id": binding["plan_id"],
        "plan_digest": binding["plan_digest"],
        "plan_version": 1,
        "revision": 1,
        "stream_id": "trial-stream",
        "provenance": "live",
        "source_label": "unit fixture only",
        "observed_at": at.isoformat(),
        "expires_at": (at + timedelta(seconds=20)).isoformat(),
        "nodes": [
            {
                "id": "api",
                "resource_uid": "unit-fixture",
                "label": "unit API",
                "detail": "fixture",
                "kind": "app",
                "lane": "source",
                "column": 0,
                "row": 0,
                "health": "unknown",
                "security": "unknown",
                "health_evidence": None,
                "security_evidence": None,
            }
        ],
        "edges": [],
        "evidence": [],
        "responses": [],
        "events": [],
    }
    key = b"q" * 32
    envelope = {"producer_id": reader_module.PRODUCER, "source_revision": REVISION, "body": body}
    envelope["hmac_sha256"] = hmac.new(
        key, reader_module.canonical(envelope), hashlib.sha256
    ).hexdigest()
    for name, value in (
        ("registration.private.json", binding),
        ("trial.private.json", {"source_revision": REVISION, "qualification_only": True}),
        ("feed.private.json", envelope),
    ):
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    path = root / "feed-key.private"
    path.write_bytes(key)
    path.chmod(0o600)
    app = reader_module.build(root, REVISION, 18136)
    return app, envelope


def authenticated(app):
    client = TestClient(app, base_url="http://127.0.0.1:18136")
    token = next(iter(app.tokens))
    response = client.get("/__open/" + token, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/__open/" + token).status_code == 404
    return client


def test_actual_plan_reader_has_no_workflow_write_authority(tmp_path):
    app, _ = setup(tmp_path)
    with authenticated(app) as client:
        assert client.get("/v1/plans/trial-plan").json()["state"] == "COLLECT"
        assert client.get("/v1/plans/trial-plan/workload").status_code == 200
        assert client.post("/v1/plans/trial-plan/approve", json={}).status_code == 403
        assert client.get("/v1/plans/other/workload").status_code == 403
        assert (
            client.get(
                "/v1/plans/trial-plan/workload", headers={"Origin": "https://foreign.invalid"}
            ).status_code
            == 403
        )


@pytest.mark.parametrize("change", ["body", "revision", "signature", "key", "foreign-plan"])
def test_untrusted_feed_refused(tmp_path, change):
    app, envelope = setup(tmp_path)
    if change == "body":
        envelope["body"]["nodes"][0]["label"] = "tampered"
    if change == "revision":
        envelope["source_revision"] = "d" * 40
    if change == "signature":
        envelope["hmac_sha256"] = "0" * 64
    if change == "foreign-plan":
        envelope["body"]["plan_id"] = "other"
    if change == "key":
        (tmp_path / "feed-key.private").write_bytes(b"z" * 32)
    (tmp_path / "feed.private.json").write_text(json.dumps(envelope))
    with authenticated(app) as client:
        assert client.get("/v1/plans/trial-plan/workload").status_code == 503


def test_expired_observation_is_not_reissued(tmp_path):
    app, envelope = setup(tmp_path)
    body = envelope["body"]
    at = datetime.now(UTC) - timedelta(minutes=3)
    body.update(observed_at=at.isoformat(), expires_at=(at + timedelta(seconds=20)).isoformat())
    del envelope["hmac_sha256"]
    envelope["hmac_sha256"] = hmac.new(
        b"q" * 32, reader_module.canonical(envelope), hashlib.sha256
    ).hexdigest()
    (tmp_path / "feed.private.json").write_text(json.dumps(envelope))
    with authenticated(app) as client:
        value = client.get("/v1/plans/trial-plan/workload").json()
        assert value["observed_at"] == body["observed_at"]
        assert datetime.fromisoformat(value["expires_at"]) < datetime.now(UTC)


def test_symlink_or_public_feed_refused(tmp_path):
    app, _ = setup(tmp_path)
    path = tmp_path / "feed.private.json"
    path.chmod(0o644)
    with authenticated(app) as client:
        assert client.get("/v1/plans/trial-plan/workload").status_code == 503
    path.chmod(0o600)
    path.rename(tmp_path / "other")
    path.symlink_to(tmp_path / "other")
    with authenticated(app) as client:
        assert client.get("/v1/plans/trial-plan/workload").status_code == 503
