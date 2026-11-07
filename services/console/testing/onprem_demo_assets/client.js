const labels = {
  idle: "시험 준비 전",
  preparing: "실제 자원 준비 중",
  ready: "정상 연결 확인 · 시연 준비됨",
  responding: "설정 위반 수집·격리·대체 전환 진행 중",
  complete: "실제 대응 확인 완료",
  stopping: "시험 자원 정리 중",
  ended: "시험 종료 · 기록 보존됨",
  attention: "정리·실행 상태 확인 필요",
  connection_failed: "개인 클러스터 접속 확인 필요",
};
const stepLabels = {
  apply_isolation: "원본 API 격리",
  activate_replacement: "대체 API 기동",
  verify_replacement: "대체 HTTP 검증",
  switch_route: "고정 시험 주소 전환",
};
const style = document.createElement("link");
style.rel = "stylesheet";
style.href = "/__demo/style.css";
document.head.append(style);
let state = null,
  busy = false,
  connectedPlan = null,
  lastSession = null;
const panel = document.createElement("section");
panel.id = "onprem-demo-controls";
panel.className = "onprem-demo";
panel.setAttribute("aria-label", "실제 온프렘 보안 대응 시연");
panel.innerHTML = `<div class="onprem-demo-head"><div><span class="onprem-demo-eyebrow">LIVE ON-PREM DEMO</span><h3>실제 워크로드 보안 대응</h3><p>전용 API에 설정 위반을 주입하고, 격리·대체 기동·경로 전환을 실제로 확인합니다.</p></div><span data-demo-phase role="status">연결 확인 중</span></div>
<div class="onprem-demo-scope"><strong>변경 범위</strong><span>새 시험 namespace 3개 · 최대 4개 무상태 Pod · 기존 앱·DB 변경 없음</span><span data-demo-time></span></div>
<label class="onprem-demo-consent"><input type="checkbox" data-demo-consent> 전용 시험의 자원 생성·설정 위반 주입·격리·대체 전환을 승인합니다. 제품 T10 승인과는 별도입니다.</label>
<div class="onprem-demo-actions"><button type="button" data-demo-action="prepare">1. 실제 시험 준비</button><button type="button" class="onprem-demo-critical" data-demo-action="respond">2. 설정 위반 주입·대응 실행</button><button type="button" data-demo-action="stop">3. 시험 종료·자원 정리</button></div>
<ol class="onprem-demo-steps"></ol><p data-demo-feedback role="status" aria-live="polite"></p><p class="onprem-demo-note">실제 Pod·NetworkPolicy·HTTP 확인 결과가 지도에 반영됩니다. 일반 사이버공격 탐지, 운영 앱의 복구, AWS→온프렘 이전 완료를 뜻하지 않습니다. 실행 결과가 불명확하면 자동 재실행하지 않습니다.</p>`;
const consent = panel.querySelector("[data-demo-consent]");
function render() {
  const phase = state?.phase;
  panel.dataset.phase = phase || "disconnected";
  panel.querySelector("[data-demo-phase]").textContent =
    labels[phase] || "서버 연결 확인 필요";
  panel.querySelector("[data-demo-time]").textContent =
    state?.remaining_seconds > 0
      ? `현재 시험 기한 ${Math.floor(state.remaining_seconds / 60)}분 ${state.remaining_seconds % 60}초`
      : "각 시험은 최대 8분 후 자동 정리";
  for (const button of panel.querySelectorAll("[data-demo-action]")) {
    const action = button.dataset.demoAction;
    button.disabled =
      busy ||
      !state ||
      !state[
        { prepare: "can_prepare", respond: "can_respond", stop: "can_stop" }[
          action
        ]
      ] ||
      (action !== "stop" && !consent.checked);
  }
  const list = panel.querySelector(".onprem-demo-steps");
  list.replaceChildren();
  for (const [action, label] of Object.entries(stepLabels)) {
    const li = document.createElement("li");
    const done = state?.steps?.includes(action);
    li.dataset.done = String(Boolean(done));
    li.textContent =
      (done ? "✓ " : "○ ") + label + (done ? " · 실제 확인" : "");
    list.append(li);
  }
  if (phase === "ended")
    panel.querySelector("[data-demo-feedback]").textContent =
      state.response_verified
        ? "대응 확인과 자원 정리가 끝났습니다. 지도는 종료된 시험의 기록이며, 다시 준비하면 새 자원으로 시연합니다."
        : "시험 자원 정리가 끝났습니다. 새 시험을 준비할 수 있습니다.";
  if (phase === "attention")
    panel.querySelector("[data-demo-feedback]").textContent =
      "자원 정리 또는 실행 결과를 확정할 수 없어 새 시험을 차단했습니다. 비공개 실행 기록을 확인해 주세요.";
  if (phase === "connection_failed")
    panel.querySelector("[data-demo-feedback]").textContent =
      "Tailscale 개인 접속과 등록된 클러스터를 확인해 주세요. 실제 자원 변경 전에 접속이 중단됐습니다.";
  if (phase === "ready" && !state.can_respond)
    panel.querySelector("[data-demo-feedback]").textContent =
      "대응 실행에 필요한 시간이 부족합니다. 시험을 종료한 뒤 새로 준비해 주세요.";
}
async function poll() {
  try {
    const response = await fetch("/__demo/status", {
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.timeout(6000),
    });
    if (!response.ok) throw new Error("unavailable");
    state = await response.json();
    if (state.session_id !== lastSession) {
      consent.checked = false;
      lastSession = state.session_id;
      panel.querySelector("[data-demo-feedback]").textContent = "";
    }
    if (state.plan_id && connectedPlan !== state.plan_id) {
      const form = document.getElementById("connect-form");
      if (typeof form?.onsubmit === "function") {
        document.getElementById("plan").value = state.plan_id;
        form.requestSubmit();
        connectedPlan = state.plan_id;
        const platform = [
          ...document.querySelectorAll("#response-workspace button"),
        ].find((b) => b.textContent.trim() === "플랫폼 관측");
        platform?.click();
      }
    }
    render();
  } catch {
    state = null;
    render();
    panel.querySelector("[data-demo-feedback]").textContent =
      "시연 서버와의 연결을 확인할 수 없습니다. 변경 요청을 자동 재전송하지 않습니다.";
  }
}
async function action(name) {
  if (busy || !state) return;
  const requestId = [...crypto.getRandomValues(new Uint8Array(16))]
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
  const body = {
    action: name,
    request_id: requestId,
    session_id: state.session_id,
    plan_digest: state.plan_digest,
    approved: name === "stop" || consent.checked,
  };
  busy = true;
  render();
  panel.querySelector("[data-demo-feedback]").textContent =
    "서버에서 현재 대상과 실행 가능 여부를 확인하고 있습니다.";
  try {
    const response = await fetch("/__demo/action", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Demo-CSRF": state.csrf,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(12000),
    });
    if (!response.ok) throw new Error("unconfirmed");
    consent.checked = false;
    panel.querySelector("[data-demo-feedback]").textContent =
      name === "prepare"
        ? "실제 자원을 준비합니다. 정상 응답이 수집되면 지도와 다음 버튼이 활성화됩니다."
        : name === "respond"
          ? "승인한 전용 시험 대상에만 위반을 주입하고 대응합니다. 각 확인은 실제 클러스터 응답을 기다립니다."
          : "현재 시험 자원의 UID를 확인하고 정리합니다.";
  } catch {
    panel.querySelector("[data-demo-feedback]").textContent =
      "요청의 반영 여부를 확인 중입니다. 자동 재실행하지 않고 현재 서버 상태를 다시 조회합니다.";
  } finally {
    busy = false;
    await poll();
  }
}
consent.addEventListener("change", render);
for (const button of panel.querySelectorAll("[data-demo-action]"))
  button.addEventListener("click", () => action(button.dataset.demoAction));
let readyAttempts = 0;
const ready = setInterval(() => {
  const host = document.getElementById("response-workspace");
  const form = document.getElementById("connect-form");
  if (host && typeof form?.onsubmit === "function") {
    clearInterval(ready);
    host.before(panel);
    document.querySelector("[data-view=topology]")?.click();
    render();
    void poll();
    const timer = setInterval(poll, 2000);
    window.addEventListener("pagehide", () => clearInterval(timer), {
      once: true,
    });
  } else if (++readyAttempts > 100) clearInterval(ready);
}, 100);
