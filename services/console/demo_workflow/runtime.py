"""Real T10 transitions and T19 rules over isolated, synthetic producer results.

This module has no cloud transport, subprocess, credential loader or configurable
backend. It deliberately cannot resume a product plan or the uncertain AI request.
"""

import json
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from bravo_gate.contract import T5Contract, fingerprint
from bravo_gate.engine import consume, evaluate
from bravo_gate.models import GateContext
from bravo_platform_events.change import ChangeApprovalService, CurrentChange
from bravo_platform_events.cutover import CurrentCutover, CutoverService, _get, _put
from bravo_platform_events.executor import ExecutionRead, ExecutionSnapshot, ExecutionStep
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import CollectionProof, PlanSpec, PlanStore
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import (
    Analysis,
    Application,
    Change,
    Evidence,
    Gate,
    Integrity,
    Proposal,
    Watch,
    WorkflowRecovery,
)
from bravo_platform_events.workflow_stages import ShiftApprovalService, shift_approval_reader

FORMAT = "sova-demo-workflow.v1"
MARKER = "demo-workflow.json"
STAGES = (
    "COLLECT",
    "ANALYZE",
    "PROPOSE",
    "REHEARSE",
    "GATE",
    "AWAIT_APPROVAL",
    "SHIFT",
    "CUTOVER",
    "WATCH",
    "DONE",
)
APPROVER = Principal("demo-human-approver", "demo-only")
WORKER = Principal("demo-local-worker", "demo-only")
CHANGE = Change("sha256:" + "c" * 64, "sha256:" + "d" * 64)


class DemoExecutor:
    """A synthetic effect in the demo SQLite file, never a T12 network client."""

    def __init__(self, runtime):
        self.runtime = runtime

    def submit(self, expected, command, *, idempotency_key):
        r = self.runtime
        if command != r.command or expected.plan_id != r.plan_id:
            raise EventError("demo_effect_scope_mismatch")
        with r.store._transaction(write=True, plan_id=r.plan_id) as c:
            if _get(c, r.plan_id, "demo:effect") is not None:
                raise EventError("demo_effect_already_created")
            now = r.now().isoformat()
            snapshot = ExecutionSnapshot(
                str(uuid4()),
                r.plan_id,
                1,
                "DONE",
                True,
                tuple(
                    ExecutionStep(s, "succeeded", 0)
                    for s in ("precheck", "pause", "compare", "commit")
                ),
                now,
                now,
            )
            _put(c, r.plan_id, "demo:effect", {"key": idempotency_key}, asdict(snapshot))
        if r.settings["scenario"] == "response-loss":
            return ExecutionRead("UNKNOWN", "demo_response_loss")
        return ExecutionRead("OBSERVED", snapshot=snapshot)

    def read(self, expected, *, previous=None):
        r = self.runtime
        if expected.plan_id != r.plan_id or expected.epoch != 1:
            return ExecutionRead("UNKNOWN", "demo_effect_scope_mismatch")
        value = r.saved("effect")
        if value is None:
            return ExecutionRead("UNKNOWN", "demo_effect_not_observed")
        value["steps"] = tuple(ExecutionStep(**item) for item in value["steps"])
        snapshot = ExecutionSnapshot(**value)
        if expected.execution_id not in (None, snapshot.execution_id):
            return ExecutionRead("UNKNOWN", "demo_effect_scope_mismatch")
        return ExecutionRead("OBSERVED", snapshot=snapshot)


class DemoWorkflow:
    @classmethod
    def create(cls, directory, *, scenario="happy", clock=None):
        if scenario not in {"happy", "gate-block", "response-loss"}:
            raise ValueError("unknown_demo_scenario")
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError("fresh_demo_directory_required")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise ValueError("fresh_demo_directory_required")
        directory.chmod(0o700)
        now = (clock or (lambda: datetime.now(UTC)))()
        settings = {
            "format": FORMAT,
            "plan_id": "demo-plan-" + uuid4().hex[:16],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "scenario": scenario,
            "synthetic": True,
        }
        with (directory / MARKER).open("x") as f:
            json.dump(settings, f)
        (directory / MARKER).chmod(0o600)
        store = PlanStore(directory / "demo.sqlite")
        (directory / "demo.sqlite").chmod(0o600)
        spec = PlanSpec(
            settings["plan_id"],
            "demo-only",
            "demo-chat",
            "demo-source",
            "demo-target",
            fingerprint(settings),
            "b" * 64,
            "demo-gate",
            "1",
        )
        store.register_plan(spec, run_id="demo-registration", occurred_at=now.isoformat())
        return cls(directory, clock=clock)

    def __init__(self, directory, *, clock=None):
        self.directory = Path(directory)
        if self.directory.is_symlink() or any(
            (self.directory / name).is_symlink() for name in (MARKER, "demo.sqlite")
        ):
            raise ValueError("owned_demo_directory_required")
        settings = json.loads((self.directory / MARKER).read_text())
        if settings.get("format") != FORMAT or settings.get("synthetic") is not True:
            raise ValueError("owned_demo_directory_required")
        if not (self.directory / "demo.sqlite").is_file():
            raise ValueError("owned_demo_database_required")
        self.settings = settings
        self.plan_id = settings["plan_id"]
        if not self.plan_id.startswith("demo-plan-"):
            raise ValueError("demo_plan_required")
        self.now = clock or (lambda: datetime.now(UTC))
        self.lock = threading.RLock()
        self.store = PlanStore(self.directory / "demo.sqlite")
        snap = self.store.snapshot(self.plan_id)
        if (
            snap["plan_digest"] != fingerprint(settings)
            or snap["source_environment_id"] != "demo-source"
            or snap["target_environment_id"] != "demo-target"
        ):
            raise ValueError("demo_plan_binding_mismatch")
        self.contract = T5Contract()
        self.binding = dict(
            plan_id=self.plan_id,
            plan_digest=snap["plan_digest"],
            diff_hash=CHANGE.diff_hash,
            image_digest=CHANGE.image_digest,
            target_environment_id="demo-target",
            gate_id="demo-gate",
            context_digest=self.gate_context().fingerprint,
            expires_at=settings["expires_at"],
        )
        self.command = dict(
            plan_id=self.plan_id,
            epoch=1,
            source=dict(environment_id="demo-source", connection_ref="demo/source"),
            target=dict(environment_id="demo-target", connection_ref="demo/target"),
            write_pause_budget_ms=30000,
            deadline=settings["expires_at"],
        )
        self.cutover = CutoverService(
            self.store,
            DemoExecutor(self),
            bindings={"demo-source": "demo/source", "demo-target": "demo/target"},
            check_access=self.check,
            clock=lambda: self.now().isoformat(),
            authority=self.authority,
        )
        self.changes = ChangeApprovalService(
            self.store,
            check_access=self.check,
            clock=lambda: self.now().isoformat(),
            authority=lambda *_a, **_k: CurrentChange(
                {k: v for k, v in self.binding.items() if k not in {"gate_id", "context_digest"}},
                "demo-rehearsal",
                "demo-verification",
                "demo-only-revision",
            ),
        )
        self.shift = ShiftApprovalService(
            self.store, check_approve=self.check, clock=lambda: self.now().isoformat()
        )
        self.recovery = WorkflowRecovery(self.cutover, read_evidence=self.evidence)

    def check(self, _c, principal, plan):
        if principal not in (APPROVER, WORKER) or plan["plan_id"] != self.plan_id:
            raise EventError("forbidden")

    def saved(self, name):
        with self.store._transaction() as c:
            value = _get(c, self.plan_id, "demo:" + name)
            return value[1] if value else None

    def save(self, name, value):
        with self.store._transaction(write=True, plan_id=self.plan_id) as c:
            # Demo artifacts may be refreshed explicitly. T10 approvals, claims,
            # checkpoints and effects are never rewritten by this helper.
            c.execute(
                "DELETE FROM plan_commands WHERE plan_id=? AND command_key=?",
                (self.plan_id, "demo:" + name),
            )
            _put(c, self.plan_id, "demo:" + name, {}, value)

    def runs(self):
        created = datetime.fromisoformat(self.settings["created_at"])
        runs = []
        for side in ("source", "target"):
            rows = []
            for test_id in self.contract.profile("demo-gate", "1")["required_test_ids"]:
                test = self.contract.catalog["tests"][test_id]
                blocked = (
                    self.settings["scenario"] == "gate-block" and side == "target" and not rows
                )
                rows.append(
                    dict(
                        test_id=test_id,
                        control_id=test["control_id"],
                        method_version=test["method_version"],
                        expected=test["expected"],
                        observed="allow" if blocked else test["expected"],
                        positive_control="pass"
                        if test["positive_control"] == "required"
                        else "not_applicable",
                        verdict="FAIL" if blocked else "PASS",
                        unknown_reason=None,
                        evidence=dict(
                            collected_at=(created - timedelta(seconds=2)).isoformat(),
                            expires_at=self.settings["expires_at"],
                            tool="demo-fixture",
                            command="synthetic_input_only",
                            output_sha256="sha256:" + "f" * 64,
                            excerpt="DEMO SYNTHETIC INPUT. No external access or security probe executed.",
                        ),
                    )
                )
            runs.append(
                dict(
                    kind="control_run",
                    schema_version="1",
                    run_id="demo-run-" + side,
                    environment=dict(id="demo-" + side, role=side, ref="demo:" + side),
                    app="demo-chat",
                    started_at=(created - timedelta(seconds=3)).isoformat(),
                    finished_at=(created - timedelta(seconds=1)).isoformat(),
                    results=rows,
                )
            )
        return runs

    def gate_context(self):
        source, target = self.runs()
        return GateContext(
            "demo-only",
            "demo-chat",
            "demo-gate",
            self.store.snapshot(self.plan_id)["plan_digest"],
            "demo-gate",
            "1",
            self.contract.catalog_digest,
            "demo-source",
            "demo:source",
            "demo-target",
            "demo:target",
            "demo-source-run",
            "demo-target-run",
            fingerprint(source),
            fingerprint(target),
        )

    def evaluate(self, *, persist=True):
        report = evaluate(self.contract, self.gate_context(), *self.runs(), now=self.now())
        if persist:
            self.save("gate", report)
        return report

    def authority(self, *_a, **_k):
        # Recompute immediately before approval/dispatch using the same context.
        # Even demo mode cannot turn FAIL, missing data or expiry into PASS.
        report = self.evaluate(persist=False)
        consume(self.contract, self.gate_context(), report, *self.runs(), now=self.now())
        return CurrentCutover(self.binding, self.command, "demo-authority-v1")

    def evidence(self, c, plan, previous, run):
        stage = plan["state"]
        if stage == "AWAIT_APPROVAL":
            return shift_approval_reader(c, plan, previous, run)
        analysis = Analysis("b" * 64, "1" * 64, "2" * 64)
        if stage == "ANALYZE":
            result = analysis
        elif stage == "PROPOSE":
            result = Proposal(
                analysis.inventory_digest, analysis.comparison_digest, CHANGE, True, True
            )
        elif stage == "REHEARSE":
            value = _get(c, self.plan_id, "demo:application")
            if value is None:
                raise EventError("demo_change_approval_required")
            result = Application(**{**value[1], "change": CHANGE})
        elif stage == "GATE":
            value = _get(c, self.plan_id, "demo:gate")
            if value is None or value[1]["decision"] != "PASS":
                raise EventError("demo_gate_blocked")
            result = Gate(CHANGE, self.binding["gate_id"], self.binding["context_digest"], "PASS")
        elif stage == "CUTOVER":
            result = Integrity(run, run is not None)
        elif stage == "WATCH":
            # SLI and control-retest inputs are synthetic, explicitly shown in UI.
            result = Watch(run, previous["receipt_id"], "PASS", "PASS")
        else:
            raise EventError("demo_stage_not_available")
        return Evidence(
            self.plan_id,
            json.loads(plan["spec_json"])["plan_digest"],
            "demo-target",
            plan["version"],
            f"demo-proof-{stage}-{plan['version']}",
            result,
        )

    def action(self, action, *, expected_version):
        with self.lock:
            if self.now() >= datetime.fromisoformat(self.settings["expires_at"]):
                raise EventError("demo_expired")
            snap = self.store.snapshot(self.plan_id)
            if type(expected_version) is not int or expected_version != snap["version"]:
                raise EventError("version_conflict")
            stage = snap["state"]
            version = snap["version"]
            if action == "approve_change" and stage == "REHEARSE":
                receipt = self.changes.approve(
                    APPROVER,
                    self.plan_id,
                    run_id="demo-rehearsal",
                    binding={
                        k: v
                        for k, v in self.binding.items()
                        if k not in {"gate_id", "context_digest"}
                    },
                    expected_version=version,
                    command_key="demo-approve-change",
                )
                self.save("change", receipt)
            elif action == "approve_shift" and stage == "AWAIT_APPROVAL":
                receipt = self.shift.approve(
                    APPROVER,
                    self.plan_id,
                    expected_version=version,
                    expires_at=self.settings["expires_at"],
                    command_key="demo-approve-shift",
                )
                self.save("shift", receipt)
            elif action == "approve_cutover" and stage == "SHIFT":
                receipt = self.cutover.approve(
                    APPROVER,
                    self.plan_id,
                    binding=self.binding,
                    expected_version=version,
                    command_key="demo-approve-cutover",
                )
                self.save("cutover", receipt)
            elif action == "execute" and stage == "SHIFT":
                receipt = self.saved("cutover")
                if receipt is None:
                    raise EventError("demo_cutover_approval_required")
                self.cutover.submit(
                    WORKER,
                    self.plan_id,
                    approval_id=receipt["approval_id"],
                    command_key="demo-execute",
                )
            elif action == "advance" and stage == "COLLECT":
                self.store.complete_collection(
                    CollectionProof(
                        self.plan_id, snap["plan_digest"], "b" * 64, "demo-collection", True
                    ),
                    expected_version=version,
                    command_key="demo-collect",
                    run_id="demo-collector",
                    occurred_at=self.now().isoformat(),
                )
            elif action == "advance" and stage in STAGES[1:6] + ("CUTOVER", "WATCH"):
                if stage == "REHEARSE":
                    approval = self.saved("change")
                    if approval is None:
                        raise EventError("demo_change_approval_required")
                    claim, _ = self.changes.claim_application(
                        WORKER,
                        self.plan_id,
                        approval_id=approval["approval_id"],
                        command_key="demo-apply",
                    )
                    self.save(
                        "application",
                        asdict(
                            Application(
                                CHANGE,
                                claim["verification_id"],
                                approval["approval_id"],
                                claim["application_id"],
                                True,
                                CHANGE.image_digest,
                            )
                        ),
                    )
                if stage == "GATE":
                    report = self.evaluate()
                    if report["decision"] != "PASS":
                        raise EventError("demo_gate_blocked")
                    self.store.append(
                        plan_id=self.plan_id,
                        run_id="demo-gate-evaluator",
                        event_type="gate.completed",
                        data=dict(
                            gate_id=report["gate_id"],
                            context_digest=report["context_digest"],
                            decision=report["decision"],
                            evidence_ref="demo-synthetic-runs",
                        ),
                        occurred_at=self.now().isoformat(),
                        producer_key="demo-gate-completed",
                    )
                self.recovery.resume_once(WORKER, self.plan_id)
            else:
                raise EventError("demo_action_not_allowed")
            return self.view()

    def view(self):
        with self.lock:
            return self._view()

    def _view(self):
        snap = self.store.snapshot(self.plan_id)
        return dict(
            mode="demo",
            synthetic=True,
            operational_acceptance=False,
            authorizes_production=False,
            scenario=self.settings["scenario"],
            plan=snap,
            expires_at=self.settings["expires_at"],
            stages=STAGES,
            approvals={
                name: self.saved(name) is not None for name in ("change", "shift", "cutover")
            },
            gate=self.saved("gate"),
            synthetic_effects=int(self.saved("effect") is not None),
            events=self.store.read_after(self.plan_id, None),
        )
