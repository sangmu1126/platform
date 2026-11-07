"""Demo qualification, not operational migration or security acceptance."""

import socket
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from demo_workflow.runtime import APPROVER, STAGES, DemoWorkflow

ACTIONS = (
    "advance",
    "advance",
    "advance",
    "approve_change",
    "advance",
    "advance",
    "approve_shift",
    "advance",
    "approve_cutover",
    "execute",
    "advance",
    "advance",
)


def perform(runtime, actions):
    for action in actions:
        runtime.action(action, expected_version=runtime.store.snapshot(runtime.plan_id)["version"])
    return runtime.view()


def test_actual_t10_sequence_and_t19_rules_without_external_effects(tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("demo_must_not_call_external_transport_or_commands")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    r = DemoWorkflow.create(tmp_path / "demo")
    result = perform(r, ACTIONS)
    assert result["plan"]["state"] == "DONE"
    transitions = [e["data"]["to"] for e in result["events"] if e["event_type"] == "state.changed"]
    assert transitions == list(STAGES)
    approvals = [e["data"] for e in result["events"] if e["event_type"] == "approval.decided"]
    assert [a["approval_kind"] for a in approvals] == ["change", "cutover"]
    assert all(a["actor_id"] == APPROVER.subject_id for a in approvals)
    assert r.saved("shift")["actor_id"] == APPROVER.subject_id
    assert len(result["gate"]["rows"]) == 3 and result["gate"]["decision"] == "PASS"
    assert result["synthetic_effects"] == 1
    assert result["synthetic"] and not result["operational_acceptance"]
    assert not result["authorizes_production"]


@pytest.mark.parametrize("index", [3, 6, 8])
def test_cannot_advance_past_human_approval(tmp_path, index):
    r = DemoWorkflow.create(tmp_path / "demo")
    perform(r, ACTIONS[:index])
    before = r.store.snapshot(r.plan_id)
    with pytest.raises(ValueError):
        r.action("execute" if index == 8 else "advance", expected_version=before["version"])
    assert r.store.snapshot(r.plan_id) == before
    assert r.saved("effect") is None


def test_target_failure_blocks_real_evaluator(tmp_path):
    r = DemoWorkflow.create(tmp_path / "demo", scenario="gate-block")
    perform(r, ACTIONS[:5])
    with pytest.raises(ValueError, match="demo_gate_blocked"):
        perform(r, ["advance"])
    assert r.view()["gate"]["decision"] == "BLOCK"
    assert r.store.snapshot(r.plan_id)["state"] == "GATE"
    assert r.saved("effect") is None


def test_missing_result_cannot_use_the_demo_to_force_pass(tmp_path, monkeypatch):
    r = DemoWorkflow.create(tmp_path / "demo")
    original = r.runs

    def missing():
        runs = original()
        runs[1]["results"].pop()
        return runs

    monkeypatch.setattr(r, "runs", missing)
    perform(r, ACTIONS[:5])
    with pytest.raises(ValueError, match="demo_gate_blocked"):
        perform(r, ["advance"])
    assert "MISSING_RESULT" in {row["reason"] for row in r.view()["gate"]["rows"]}


def test_bound_evidence_is_checked_by_existing_recovery(tmp_path, monkeypatch):
    r = DemoWorkflow.create(tmp_path / "demo")
    perform(r, ["advance"])
    reader = r.recovery.read_evidence

    def foreign(*args):
        return replace(reader(*args), plan_id="demo-plan-foreign")

    monkeypatch.setattr(r.recovery, "read_evidence", foreign)
    with pytest.raises(ValueError, match="workflow_evidence_unavailable"):
        perform(r, ["advance"])
    assert r.store.snapshot(r.plan_id)["state"] == "ANALYZE"


def test_restart_reuses_plan_approvals_and_single_effect(tmp_path):
    directory = tmp_path / "demo"
    r = DemoWorkflow.create(directory)
    perform(r, ACTIONS[:10])
    effect = r.saved("effect")
    original_plan = r.plan_id
    r = DemoWorkflow(directory)
    assert r.plan_id == original_plan and r.saved("effect") == effect
    result = perform(r, ACTIONS[10:])
    assert result["plan"]["state"] == "DONE" and result["synthetic_effects"] == 1
    with pytest.raises(ValueError, match="demo_action_not_allowed"):
        perform(r, ["execute"])
    assert r.saved("effect") == effect


def test_same_version_double_click_has_only_one_transition(tmp_path):
    r = DemoWorkflow.create(tmp_path / "demo")
    r.action("advance", expected_version=1)
    with pytest.raises(ValueError, match="version_conflict"):
        r.action("advance", expected_version=1)
    assert r.store.snapshot(r.plan_id)["state"] == "ANALYZE"


def test_dispatch_rechecks_evidence_after_human_approval(tmp_path, monkeypatch):
    r = DemoWorkflow.create(tmp_path / "demo")
    perform(r, ACTIONS[:9])
    original = r.runs

    def changed():
        runs = original()
        runs[1]["results"][0].update(observed="allow", verdict="FAIL")
        return runs

    monkeypatch.setattr(r, "runs", changed)
    with pytest.raises(ValueError, match="authority_unavailable"):
        perform(r, ["execute"])
    assert r.store.snapshot(r.plan_id)["state"] == "SHIFT"
    assert r.saved("effect") is None


def test_lost_effect_response_holds_even_after_restart(tmp_path):
    directory = tmp_path / "demo"
    r = DemoWorkflow.create(directory, scenario="response-loss")
    perform(r, ACTIONS[:10])
    assert r.store.snapshot(r.plan_id)["state"] == "SAFE_HOLD"
    r = DemoWorkflow(directory)
    assert r.saved("effect") is not None
    with pytest.raises(ValueError, match="demo_action_not_allowed"):
        perform(r, ["execute"])
    assert r.view()["synthetic_effects"] == 1 and r.view()["plan"]["state"] == "SAFE_HOLD"


def test_expired_demo_and_approval_cannot_be_extended_by_resume(tmp_path):
    state = {"now": datetime.now(UTC)}
    r = DemoWorkflow.create(tmp_path / "demo", clock=lambda: state["now"])
    perform(r, ACTIONS[:9])
    state["now"] += timedelta(hours=2)
    r = DemoWorkflow(tmp_path / "demo", clock=lambda: state["now"])
    with pytest.raises(ValueError, match="demo_expired"):
        perform(r, ["execute"])
    assert r.saved("effect") is None


def test_unowned_directory_and_product_database_are_rejected(tmp_path):
    directory = tmp_path / "product"
    directory.mkdir()
    (directory / "real.sqlite").write_bytes(b"original product data")
    with pytest.raises(ValueError, match="fresh_demo_directory_required"):
        DemoWorkflow.create(directory)
    with pytest.raises(FileNotFoundError):
        DemoWorkflow(directory)
    assert (directory / "real.sqlite").read_bytes() == b"original product data"


def test_symlink_resume_rejected(tmp_path):
    r = DemoWorkflow.create(tmp_path / "demo")
    (tmp_path / "alias").symlink_to(r.directory)
    with pytest.raises(ValueError, match="owned_demo_directory_required"):
        DemoWorkflow(tmp_path / "alias")
