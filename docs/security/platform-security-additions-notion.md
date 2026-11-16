# Patching as a Service 플랫폼 적용 현황 — 구현 완료와 실환경 HOLD 구분

> 최종 갱신: 2026-11-08 KST
>
> 관련 기준 문서: **보안&거버넌스 설계 v1**, **Patching as a Service**
>
> 이 문서의 목적: 기존 설계를 반복하지 않고, 팀 저장소 전체에서 실제로 추가 설계·구현·병합한 내용과 현재 실환경 인수 상태를 구분해 기록한다.

## 1. 핵심 결론

Patching as a Service는 단순한 취약점 스캐너나 자동 패치 봇이 아니다.

플랫폼이 취약점 정보를 받아 수정안을 만들고, 격리된 T15 환경에서 검증한 뒤, 기존 T31 변경 승인과 T10 컷오버 권한에 보안 근거를 추가하는 **증적 기반 변경 통제 경로**로 구현했다.

다만 다음 세 상태를 반드시 구분해야 한다.

| 구분 | 현재 상태 |
|---|---|
| 설계 및 코드 구현 | 완료 |
| 관련 PR 병합 및 CI 검증 | 완료 |
| 현재 플랫폼 클러스터 배포 | 일부 완료 |
| Security Review 실서비스 배포 | 미완료 |
| 실제 TokenReview·TLS·NetworkPolicy 검증 | 미완료 |
| 실제 Plan의 `prepare-change`·`prepare-cutover` 증적 | 미완료 |
| T24 감사 연결 및 운영 인수 | 미완료 |

따라서 현재 판정은 다음과 같다.

> **구현 및 병합 완료 / 실환경 운영 인수 HOLD**

코드가 병합됐다는 사실만으로 운영 PASS를 선언하지 않는다.

---

## 2. 기존 문서와 이 문서의 차이

### 보안&거버넌스 설계 v1

플랫폼 전체의 권한 분리, 신원, 감사, 네트워크 및 승인 원칙을 정의한 상위 설계 문서다.

### Patching as a Service

취약점 탐지부터 수정안 생성, 격리 검증, 승인, 배포, 사후 검증까지의 제품·운영 흐름을 정의한 서비스 설계 문서다.

### 이 문서

두 설계를 플랫폼 코드와 운영 경로에 실제로 어떻게 연결했는지, 어떤 PR이 병합됐는지, 현재 클러스터에 무엇이 없어서 인수가 보류되는지를 기록한다.

즉 내용이 일부 연결되지만 같은 문서는 아니다.

---

## 3. 최종 설계

전체 흐름은 다음과 같다.

```text
취약점·정책 입력
      |
      v
AI 수정안 생성
      |
      v
T15 격리 리허설·검증
      |
      v
patch/preflight Security Review
      |
      v
기존 T31 사람 승인
      |
      v
기존 Executor 적용
      |
      v
Post-Apply T19 평가
      |
      +--> patch/postflight
      +--> migration/preflight
                  |
                  v
           기존 T10 컷오버 승인
                  |
                  v
              T24 감사
```

핵심은 새로운 보안 승인 시스템을 만들지 않았다는 점이다.

- Security Review는 정책과 근거의 적합성을 판정한다.
- T31은 변경의 최종 승인 권한을 유지한다.
- T10은 컷오버의 최종 승인 권한을 유지한다.
- Executor만 실제 명령을 실행한다.
- T36은 오류율·p95에 따른 SHIFT/WATCH 판정을 유지한다.
- T24는 등록, 검토, 사람 승인, 실행 Receipt의 연결을 감사한다.

Security Review 응답의 `execution_authorized`는 항상 `false`다.

---

## 4. 구현한 플랫폼 권한 연결

### 4.1 T31 변경 승인

T31 승인 및 Application Claim 직전에 `patch/preflight`를 강제한다.

1. 기존 T31 Authority가 현재 Plan과 승인 조건을 평가한다.
2. 같은 Plan Transaction에서 T19 Session과 보안 프로그램 등록을 읽는다.
3. Security Review에 `mode=enforce` 요청을 보낸다.
4. PASS, 유효기간, 결속 정보를 확인한다.
5. 원격 호출 뒤 Authority, Session, Registration Revision, Fingerprint를 다시 읽는다.
6. 검토 중 값이 바뀌지 않았을 때만 기존 T31 결과를 반환한다.

등록 누락, 철회, 만료, 통신 장애 또는 상태 변경은 모두 HOLD다.

### 4.2 T10 컷오버 승인

SHIFT 100% 이후 T10 컷오버 승인 직전에 다음 근거를 모두 요구한다.

| 근거 | 결속 대상 |
|---|---|
| `patch/postflight` | 실제 Apply 결과와 Post-Apply T19 Session |
| `migration/preflight` | 현재 Plan, 환경, 프로필, Application |

둘 중 하나라도 없거나 오래됐거나 현재 상태와 다르면 컷오버를 보류한다.

### 4.3 T36과의 경계

Security Review는 오류율과 p95를 다시 계산하지 않는다. SHIFT/WATCH 판정도 대신하지 않는다.

보안 근거가 PASS여도 T36 관측 계약을 통과하지 못하면 진행할 수 없다. 반대로 T36이 정상이어도 필수 보안 근거가 없으면 T31/T10에서 진행할 수 없다.

---

## 5. 등록 원장과 동시성 방어

별도 승인 DB를 만들지 않고 기존 `plan_commands`에 보안 프로그램 등록과 철회를 append-only로 기록한다.

```text
security-program-<kind>-<phase>-<13자리 revision>
```

### 추가 권한

| 권한 | 역할 |
|---|---|
| `security:read` | 현재 등록·철회 상태 조회 및 검토 |
| `security:register` | 등록·철회를 append-only로 기록 |

일반 `read`, T31 승인 또는 T10 컷오버 권한만으로 보안 근거를 등록할 수 없다.

등록은 다음 값에 결속된다.

- Tenant와 Application
- Plan ID와 Plan Digest
- Source 및 Target Environment
- T19 Profile ID와 Version
- T19 Session Fingerprint와 Gate Phase
- 실제 Application ID
- 플랫폼 이미지 Source SHA

원격 Review 전후로 이 값들을 다시 확인해 TOCTOU와 오래된 PASS 재사용을 차단한다.

---

## 6. Fail-Closed 계약

다음 조건에서는 기본값이나 과거 PASS로 진행하지 않는다.

### 등록·결속 실패

- 등록 누락 또는 철회
- Stale Revision
- Command Key 충돌
- Plan·Tenant·Application 불일치
- Source·Target Environment 불일치
- Profile·Session·Fingerprint 불일치
- 플랫폼 이미지 Source SHA 불일치

### 검토 실패

- Review 결과 FAIL
- 필수 Security Control 누락
- 응답 유효기간 만료
- Review 도중 Authority·Session·Registration 변경
- 확인된 침해 상태가 종료되지 않음

### 인증·통신 실패

- Security Review 서비스 장애
- CA·TLS·Hostname 검증 실패
- Projected Token 누락 또는 만료
- Token Audience 불일치
- NetworkPolicy에 허용되지 않은 통신
- 제한 시간 내 결과를 확정하지 못한 UNKNOWN

실패 시 안전한 동작은 우회가 아니라 HOLD다.

---

## 7. Workload Identity와 네트워크 보안

Security Review 호출에는 기본 ServiceAccount Token을 자동 마운트하지 않는다.

| 항목 | 설계 값 |
|---|---|
| Token 경로 | `/var/run/secrets/security-review/token` |
| Audience | `registered-security-review-audience` |
| 만료 | 600초 |
| CA | `security-review-ca` Secret의 `ca.crt` |
| TLS Hostname 검증 | 필수 |
| Insecure TLS 우회 | 금지 |

T10 Intake Token과 Security Review Token은 경로와 Audience를 분리한다.

네트워크는 `t15-rehearsal-host`에서 `security-review:8443`으로 가는 호출만 추가 허용하고, Security Review Ingress는 지정 호출자만 받도록 설계했다. 기존 Default-Deny와 DNS·DB·Executor·Tailscale 경계는 유지한다.

Security Review Pod에는 Executor 자격증명을 제공하지 않는다.

---

## 8. AI 수정안과 4분 30초 데모 계약

AI 수정안 생성 경로는 데모 실행기에 연결했다.

```text
AI proposal start/collect
  -> T15 검증
  -> patch/preflight 등록·Enforce Probe
  -> 기존 사람 승인
```

### 시간 계약

- 전체 가시 데모의 하드 상한은 **270초**다.
- 보안 준비 단계의 하위 예산은 **45초**다.
- 최초 실행 시 시작 시각과 270초 예산을 JSONL 증적에 고정한다.
- 사람 승인 후 재개해도 최초 시작 시각을 유지한다.
- 각 명령과 조회는 전체 잔여 시간보다 오래 실행할 수 없다.
- 제한 시간을 끄는 옵션은 없다.
- TIMEOUT 또는 UNKNOWN 뒤 같은 효과를 자동 재시도하지 않는다.
- 시간 부족을 이유로 T15, Security Review 또는 사람 승인을 생략하지 않는다.

### 워밍

데모 전에 다음 명령으로 필요한 Deployment와 연결을 준비한다.

```bash
python3 operations/security-program/run.py warm \
  --context <platform> \
  --budget-seconds 45
```

워밍은 승인이나 실행을 대신하지 않는다. 데모 본 흐름의 cold start를 줄이고, 필수 서비스가 준비되지 않았으면 시작 전에 fail closed로 발견하기 위한 절차다.

### 270초 안에 끝나지 않을 때

데모 성공으로 위장하지 않는다. `demo_budget_exceeded` 또는 해당 실패 상태를 증적에 남기고, 사전에 준비한 실패 화면과 설명으로 전환한다.

---

## 9. 공급망과 이미지 무결성

외부 보안 패키지를 Branch 최신 상태로 가져오지 않고 Exact Commit으로 고정한다.

| 항목 | 값 |
|---|---|
| Package | `sova-security` |
| 반영 Security SHA | `cf33e09c8d3be567542a17fe039f3e6e2295568d` |
| 반영 내용 | PHP2Shell 방어 회귀 포함 |

빌드 과정은 다음을 확인한다.

1. 지정 Commit만 Shallow Fetch한다.
2. `FETCH_HEAD`와 기대 SHA가 일치해야 한다.
3. 허용된 일반 Git Blob만 이미지에 포함한다.
4. Git Metadata, Symlink, Uncommitted File을 제외한다.
5. Build Receipt에 Security Revision을 기록한다.
6. Approval Image의 Import Closure를 빌드 단계에서 검증한다.

이를 통해 로컬 작업 디렉터리나 임의의 최신 Branch가 승인 경로에 섞이는 것을 막는다.

---

## 10. PHP2Shell AI 모의해킹 사례 반영

MBC가 보도한 허가된 AI 모의해킹의 구버전 PHP 서버 장악 사례를 방어 요구사항으로 반영했다.

참고 기사: <https://imnews.imbc.com/replay/2026/nwdesk/article/6863935_37004.html>

기사가 정확한 공격 명령과 대상 Build/SAPI를 공개하지 않았으므로 기사 속 Exploit을 임의로 단정하지 않는다. 대신 공식 PHP RCE 취약점과 수정 경계를 방어 Fixture로 사용한다.

### 추가한 방어 계약

- PHP-FPM, `mod_php`, `php-cgi`, FrankenPHP, LiteSpeed를 Inventory 범위에 포함
- 공식 수정 버전 또는 검증된 Vendor Backport만 수정 근거로 인정
- WAF나 설정 완화만으로 PASS를 부여하지 않음
- `risk-assessment` FAIL이면 T31 정상 변경 결과를 차단
- 확인된 침해는 `EMERGENCY / preserve-rebuild-rotate`로 분류
- 버전 수정과 Finding 제거 뒤에도 침해 조사가 끝나지 않으면 HOLD 유지
- 침해 확인 시 증거 보존, Clean Rebuild, 자격증명 회전을 요구
- 공격 Payload, Webshell, 외부 무단 Scan 기능은 구현하지 않음

상세 문서: `docs/security/php2shell-ai-pentest-incident.md`

---

## 11. 정부 AI 보안·거버넌스 자료 참조

정부 자료는 외부 통제 기준과 설계 Cross-check 용도로만 참조한다.

### 과학기술정보통신부·KISA

**에이전틱 AI 제로트러스트 안내서**

- 지속적 검증
- 비인간 신원 통제
- 최소 권한
- 외부 도구·통신 통제
- 위험 작업의 사람 승인

플랫폼에서는 Fresh Review, 전용 Workload Identity, 권한 분리, NetworkPolicy, 사람 승인 유지로 연결했다.

### 행정안전부

**공공부문 인공지능 윤리기준**, **공공부문 AI 도입·활용 가이드**

- 안전성, 책임성, 투명성
- 기획·구축·운영 수명주기 관리
- 책임 주체 유지
- 운영 증적 확보

플랫폼에서는 Preflight/Postflight 분리, 등록·철회 원장, 배포 전 Probe, 운영 인수 체크와 감사 증적으로 연결했다.

이 참조만으로 법적 적합성, 정부 인증 또는 지침 전체 준수를 주장하지 않는다. 개인정보·민감정보가 Review 입력에 포함되면 별도의 영향평가와 보유·파기 정책이 필요하다.

---

## 12. 병합 및 검증 현황

### 병합 완료

| 저장소·PR | 반영 내용 | 상태 |
|---|---|---|
| security PR #4 | PHP2Shell 방어 회귀 | 병합 |
| platform PR #924 | 보안 거버넌스와 Patching as a Service 기반 연결 | 병합 |
| platform PR #939 | AI 수정안 경로와 270초 데모 계약 | 병합 |
| platform PR #912 | T33 Findings 네트워크 경로 | 병합 |

주요 Platform Merge SHA:

- PR #924: `2e2471e674a644642f8f52fb9cb02bbe2268d017`
- PR #939: `1708b6254a818807f336e6d59bd96a9c894f1dd0`
- PR #912: `2f144b45614ae8868b4ffca86aad0f75f4a5579a`

### 검증 결과

| 검증 | 결과 |
|---|---|
| PR #939 CI | 31 checks passed |
| Happy Path 집중 테스트 | 21 passed |
| 보안 준비 경로 집중 테스트 | 20 passed |
| SQLite·TLS 포함 확장 테스트 | 176 passed |
| PostgreSQL 의존 테스트 | 로컬 `pg_config` 부재로 147 deselected |

`pg_config` 부재를 전체 테스트 PASS로 기록하지 않는다. 동시에 해당 로컬 의존성 중단을 변경 코드의 테스트 실패로도 오기하지 않는다.

---

## 13. 2026-11-08 실환경 관측 결과

플랫폼 클러스터에서 직접 확인한 사실은 다음과 같다.

### 접속과 클러스터 식별

- `kube-system` UID: `5d978ed5-52a9-405a-a4ce-fd2e1c3ac9f9`
- 확인 Identity: 운영자 개인 Google 계정(공개 문서에서는 주소를 가림)
- 확인 Group: `bravo:operators` 포함

### 현재 T15

- `t15-rehearsal-host`: Ready `1/1`
- 현재 이미지 Digest: `sha256:84d80ea356e9dae01f89ac78dcdcd521bf0e955234c928f2d4db4f769837ef3d`
- 현재 Pin의 Source Revision: `db16f1403e125ae7831d07e63318f818790242bf`
- 현재 Deployment에는 `security-review-ca` 및 `security-review-token` Volume/Mount가 없음

즉 T15 자체는 살아 있지만 새 Security Review 연동 구성이 배포된 상태는 아니다.

### 현재 없는 실환경 리소스

- `Deployment/security-review`: 없음
- `Service/security-review`: 없음
- Security Review EndpointSlice: 없음
- `Secret/security-review-ca`: 없음
- Security Review 전용 NetworkPolicy: 없음

따라서 현재 클러스터에서는 실제 TLS, Projected Token, TokenReview, Service Endpoint 및 NetworkPolicy 대조군을 검증할 수 없다.

---

## 14. Platform PR #948을 HOLD한 이유

PR #939 병합 뒤 새 Rehearsal Host 이미지 Pin PR #948이 자동 생성됐다.

그러나 새 이미지가 요구하는 Security Review 서비스, CA, Token Projection 및 Operator 설정이 아직 플랫폼에 없으므로 바로 병합하면 기존 T15 Rollout을 깨뜨릴 위험이 있다.

따라서 PR #948은 Draft/HOLD로 두었다.

이 HOLD는 구현 실패가 아니라 안전한 배포 순서를 지키기 위한 조치다.

필수 선행 조건은 다음과 같다.

1. Security Review 이미지를 발행하고 Digest를 고정한다.
2. 전용 DB Reader와 Workload Grant를 준비한다.
3. TLS 인증서와 `security-review-ca`를 배포한다.
4. `Service/security-review`와 Endpoint를 준비한다.
5. 호출자 Egress와 서비스 Ingress NetworkPolicy를 적용한다.
6. 보호된 `operator.json`과 Projected Token 설정을 배포한다.
7. 정상·차단 대조군을 확인한다.
8. 그 다음 PR #948의 T15 이미지 Pin을 병합한다.

---

## 15. 아직 완료되지 않은 것

### 실환경 배포

- Security Review 이미지 발행
- Security Review Deployment·Service·Endpoint 배포
- TLS Secret과 CA 배포
- 전용 DB Reader와 Workload Grant 구성
- Security Review 전용 NetworkPolicy 적용
- 새 T15 이미지 Pin PR #948 병합

### 실환경 검증

- Projected Token 실제 TokenReview
- Audience와 만료 검증
- 정상 TLS·Hostname 검증
- 잘못된 CA 차단 대조군
- 허용·차단 NetworkPolicy 대조군
- 실제 Plan의 `prepare-change` PASS
- 실제 Apply 후 `prepare-cutover` PASS
- 등록 누락·철회·서비스 장애 시 HOLD
- 실패 경로에서 Executor 호출 0회 확인

### 운영 인수

- 등록 → Review → 사람 승인 → 실행 Receipt 연결
- T24 감사 증적 확인
- 운영 주체와 장애 대응 책임 확정
- Credential·인증서 Rotation 책임 확정
- 270초 실데모 리허설 증적 확보

---

## 16. 실환경 완료 순서

1. Security Review 컨테이너를 빌드·스캔·발행하고 Digest를 고정한다.
2. 전용 DB Reader, Grant, TLS Secret, CA 및 보호 설정을 준비한다.
3. Security Review Deployment와 Service를 배포한다.
4. TokenReview와 Audience를 확인한다.
5. NetworkPolicy 정상·차단 대조군을 실행한다.
6. 새 T15 이미지 Pin PR #948을 검토·병합한다.
7. `warm`으로 두 Deployment의 준비 상태를 확인한다.
8. 실제 Plan으로 `prepare-change`를 실행한다.
9. 실제 Apply 및 Post-Apply T19 평가 뒤 `prepare-cutover`를 실행한다.
10. 실패 대조군에서 HOLD와 Executor 0회를 확인한다.
11. T24 감사 연결과 270초 데모 리허설을 확인한다.
12. 모든 증적이 있을 때만 운영 인수를 PASS로 전환한다.

---

## 17. 주요 구현 파일

| 파일 | 역할 |
|---|---|
| `services/platform-events/src/bravo_platform_events/security_governance.py` | 등록 원장, 결속 검증, 원격 Review Authority |
| `services/platform-events/src/bravo_platform_events/security_relay.py` | 등록·철회·Probe Relay |
| `services/platform-events/src/bravo_platform_events/operator_ports.py` | T31/T10 Authority 연결 |
| `services/platform-events/src/bravo_platform_events/operator_access.py` | 보안 권한 분리 |
| `operations/security-program/run.py` | 워밍, 등록, 철회, Enforce Probe, 준비 CLI |
| `scripts/happy_path.py` | AI 제안부터 승인까지의 270초 데모 실행기 |
| `infra/charts/rehearsal-host/templates/deployment.yaml` | CA 및 Projected Token 마운트 |
| `infra/envs/rehearsal-access/rehearsal-host-network.yaml` | 호출자·서비스 네트워크 경계 |
| `services/platform-events/deploy/product-read/build.py` | Exact Commit Pin과 이미지 포함 검증 |
| `services/platform-events/tests/test_security_governance.py` | 등록·권한·Fail-Closed 테스트 |
| `services/platform-events/tests/test_security_program_run.py` | 준비 순서·제한 시간·실패 테스트 |
| `docs/security/php2shell-ai-pentest-incident.md` | PHP2Shell 사고 대응 계약 |
| `docs/operations/t28-demo-runbook.md` | 270초 데모 운영 절차 |

---

## 18. 최종 판정

### 완료된 것

- Patching as a Service의 플랫폼 권한 경로 설계
- T31/T10 추가 보안 조건과 Fail-Closed 구현
- 등록·철회 원장과 동시성 방어
- Workload Identity·TLS·NetworkPolicy 차트 구성
- AI 수정안 → T15 → Security Review → 사람 승인 경로
- 270초 전체 데모 및 45초 보안 하위 예산
- 외부 보안 패키지 Exact Commit Pin
- PHP2Shell 방어 회귀
- 관련 PR 병합과 CI 검증

### 완료되지 않은 것

- Security Review 실서비스 배포
- 새 T15 이미지의 안전한 배포
- 실제 TokenReview·TLS·NetworkPolicy 증적
- 실제 Plan의 Preflight·Postflight 증적
- T24 감사 연결
- 270초 실환경 리허설과 운영 인수

> **최종 상태: 설계·구현·병합 완료 / 실환경 운영 인수 HOLD**

남은 작업의 목표는 기능을 더 추가하는 것이 아니라, 이미 구현한 권한·신원·통신·감사 계약을 실제 플랫폼 리소스와 증적으로 닫는 것이다.
