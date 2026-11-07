const $ = (id) => document.getElementById(id);
const labels = {
  COLLECT: "조사",
  ANALYZE: "분석",
  PROPOSE: "AI 제안",
  REHEARSE: "리허설",
  GATE: "보안 게이트",
  AWAIT_APPROVAL: "전환 시작 승인",
  SHIFT: "전환 준비",
  CUTOVER: "전환",
  WATCH: "검증·관측",
  DONE: "데모 완료",
  SAFE_HOLD: "안전 보류",
};
const errors = {
  demo_gate_blocked:
    "대상 시험이 실패해 게이트가 전환을 차단했습니다. 데모에서도 이 결과를 강제로 통과시키지 않습니다.",
  demo_change_approval_required:
    "변경 내용을 사람이 승인해야 진행할 수 있습니다.",
  shift_approval_required: "전환 시작 승인이 필요합니다.",
  demo_cutover_approval_required: "컷오버 승인이 필요합니다.",
  demo_expired: "데모 유효기간이 끝났습니다. 새 데모를 시작해 주세요.",
  version_conflict:
    "다른 요청으로 계획이 바뀌었습니다. 현재 상태를 다시 조회했습니다.",
};
let current = null,
  busy = false,
  connected = false,
  timer = null;
function nextAction(value) {
  const state = value.plan.state;
  if (state === "REHEARSE" && !value.approvals.change)
    return ["approve_change", "변경 승인"];
  if (state === "AWAIT_APPROVAL" && !value.approvals.shift)
    return ["approve_shift", "전환 시작 승인"];
  if (state === "SHIFT")
    return value.approvals.cutover
      ? ["execute", "데모 전환 실행"]
      : ["approve_cutover", "컷오버 승인"];
  if (["DONE", "SAFE_HOLD", "NEEDS_HUMAN"].includes(state)) return null;
  return ["advance", "다음 단계"];
}
function description(value) {
  const state = value.plan.state;
  if (state === "DONE")
    return "같은 데모 계획의 전체 흐름을 완료했습니다. 실제 플랫폼 이전 완료를 의미하지 않습니다.";
  if (state === "SAFE_HOLD")
    return "합성 실행 결과의 응답 유실로 안전 보류됐습니다. 효과는 저장됐지만 완료로 바꾸거나 다시 실행하지 않습니다.";
  if (state === "REHEARSE")
    return "합성 리허설 결과를 확인하고 변경을 승인합니다. 승인은 기존 T10 원장에 저장됩니다.";
  if (state === "GATE")
    return "합성 양쪽 시험 결과를 실제 T19 규칙으로 비교합니다. 누락·실패·만료는 차단됩니다.";
  if (state === "AWAIT_APPROVAL")
    return "게이트 통과 이후에도 전환 시작에 사람 승인이 필요합니다.";
  if (state === "SHIFT")
    return "컷오버 승인과 실행을 구분합니다. 실행 직전에 합성 증거를 다시 평가합니다.";
  if (state === "WATCH")
    return "전환 후 합성 무결성·관측 입력을 같은 실행에 연결합니다.";
  return "외부 AI·리허설 호스트를 호출하지 않고, 합성 결과를 기존 상태 머신에 연결합니다.";
}
function render(value) {
  current = value;
  const stage = value.plan.state;
  const visited = new Set(
    value.events
      .filter((e) => e.event_type === "state.changed")
      .map((e) => e.data.to),
  );
  $("demo-stages").replaceChildren(
    ...value.stages.map((state) => {
      const li = document.createElement("li");
      li.textContent = labels[state];
      li.className =
        state === stage ? "current" : visited.has(state) ? "visited" : "";
      if (state === stage) li.setAttribute("aria-current", "step");
      return li;
    }),
  );
  $("demo-description").textContent = description(value);
  $("demo-current").textContent =
    `${labels[stage] || stage} · v${value.plan.version} · 합성 효과 ${value.synthetic_effects}회`;
  const next = nextAction(value);
  $("demo-next").textContent =
    next?.[1] || (stage === "DONE" ? "데모 완료" : "중단됨");
  $("demo-next").disabled = busy || !next;
  $("demo-auto").disabled = busy || !next || next[0] !== "advance";
  $("demo-gate").replaceChildren(
    ...(value.gate ? value.gate.rows : []).map((row) => {
      const li = document.createElement("li");
      li.textContent = `${row.test_id}: ${row.source_verdict} → ${row.target_verdict} / ${row.decision}`;
      li.className = row.decision === "BLOCK" ? "demo-failed" : "";
      return li;
    }),
  );
  if (!value.gate) $("demo-gate").textContent = "아직 평가하지 않았습니다.";
  $("demo-events").replaceChildren(
    ...value.events.slice(-18).map((event) => {
      const li = document.createElement("li");
      const data = event.data;
      const detail =
        event.event_type === "state.changed"
          ? `${labels[data.from] || "등록"} → ${labels[data.to] || data.to}`
          : event.event_type === "approval.decided"
            ? `${data.approval_kind} 승인 · ${data.actor_id}`
            : event.event_type === "gate.completed"
              ? `합성 증거 판정 ${data.decision}`
              : `${event.event_type} · ${data.step || ""} ${data.status || ""}`;
      li.textContent = `${event.sequence}. ${detail}`;
      return li;
    }),
  );
  if (!connected) {
    connected = true;
    $("plan").value = value.plan.plan_id;
    $("connect-form").requestSubmit();
  }
}
async function refresh() {
  const response = await fetch("/__demo/status", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error("데모 접속이 만료됐거나 상태를 읽을 수 없습니다.");
  render(await response.json());
}
async function act(action) {
  const response = await fetch("/__demo/action", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Sova-Demo": "1" },
    body: JSON.stringify({ action, expected_version: current.plan.version }),
  });
  const result = await response.json();
  if (!response.ok) {
    await refresh();
    throw new Error(
      errors[result.error] ||
        "데모 조건을 충족하지 못했습니다. 현재 단계와 승인을 확인해 주세요.",
    );
  }
  render(result);
}
async function perform(automatic) {
  if (busy || !current) return;
  let next = nextAction(current);
  if (!next) return;
  if (
    !automatic &&
    next[0] !== "advance" &&
    !window.confirm(
      `${next[1]}을 진행할까요? 합성 계획에만 적용되며 운영 환경에는 실행되지 않습니다.`,
    )
  )
    return;
  busy = true;
  $("demo-error").textContent = "";
  render(current);
  try {
    for (let count = 0; count < 10; count++) {
      next = nextAction(current);
      if (!next || (automatic && next[0] !== "advance")) break;
      await act(next[0]);
      if (!automatic) break;
      await new Promise((resolve) => setTimeout(resolve, 650));
    }
  } catch (error) {
    $("demo-error").textContent = error.message;
  } finally {
    busy = false;
    if (current) render(current);
  }
}
$("demo-next").addEventListener("click", () => perform(false));
$("demo-auto").addEventListener("click", () => perform(true));
async function poll() {
  try {
    if (!busy) await refresh();
  } catch (error) {
    $("demo-error").textContent = error.message;
    $("demo-next").disabled = true;
    $("demo-auto").disabled = true;
    return;
  }
  timer = setTimeout(poll, 1500);
}
window.addEventListener("pagehide", () => clearTimeout(timer));
poll();
