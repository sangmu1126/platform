# Sova 데모 전용 워크플로우

기존 Sova 콘솔에서 조사부터 완료까지 같은 계획의 흐름을 시연한다. 외부 AI·리허설·시험·전환·관측 결과는 **합성 입력**이다. 기존 T10의 상태 머신·승인·실행 접수·이벤트 저장과 T19의 비교 규칙을 사용한다. 운영 해피패스의 성공 기록이나 제품의 보안 예외 기능이 아니다.

## 실행

이 문서의 기준은 `feature/T21-demo-workflow`다. 콘솔의 잠금 파일과 로컬 의존성을 설치한다.

```sh
cd services/console
uv sync --locked --group dev
uv run --no-sync python -m demo_workflow.server
```

서버는 `127.0.0.1:18131`에서만 열린다. 시작 출력에 나온 개인 `access.private.json`의 `bootstrap_url`을 로컬 브라우저에서 한 번 연다. URL·쿠키·접속 파일을 커밋하거나 메시지에 게시하지 않는다. 로그인은 로컬 데모 세션이며 기업 로그인이나 플랫폼 운영자 인증이 아니다.

새 데모는 기본적으로 별도의 임시 디렉터리·SQLite 파일·`demo-plan-*` 계획을 만든다. DB·세션·합성 증거·승인은 최대 2시간이며 재시작으로 연장되지 않는다. 기록은 자동 삭제하지 않는다.

```sh
# 저장 위치를 명시하려면 새 디렉터리 또는 빈 디렉터리만 지정한다.
uv run --no-sync python -m demo_workflow.server --new /tmp/sova-demo-my-run

# 같은 데모 디렉터리에서 기존 계획과 승인·효과를 이어서 읽는다.
uv run --no-sync python -m demo_workflow.server --resume /tmp/sova-demo-my-run
```

재시작하면 새 일회용 접속 주소가 생성된다. 원래 데모가 만료됐다면 새 데모를 시작한다. 운영 DB나 DSN, 클러스터 설정, AI 키, 임의 호스트 주소를 받는 옵션은 없다.

## 시연 순서

```text
COLLECT → ANALYZE → PROPOSE → REHEARSE → GATE
           → AWAIT_APPROVAL → SHIFT → CUTOVER → WATCH → DONE
```

| 화면 조작 | 처리 |
|---|---|
| 승인 전까지 진행 | 합성 수집·분석·AI 제안을 T10에 연결하고 리허설의 변경 승인 앞에서 정지 |
| 변경 승인 | 실제 `ChangeApprovalService`에 로컬 사람의 데모 승인을 저장 |
| 승인 전까지 진행 | 합성 적용 결과를 같은 승인·이미지 digest에 결속. T19 규칙으로 합성 양쪽 시험 3개를 비교 |
| 전환 시작 승인 | 실제 `ShiftApprovalService`에 현재 게이트의 승인을 저장 |
| 다음 단계 | 기존 복구 드라이버가 AWAIT_APPROVAL → SHIFT로 전이 |
| 컷오버 승인 | 실제 `CutoverService`가 현재 계획·버전·게이트·기한에 결속한 승인을 저장 |
| 데모 전환 실행 | 증거를 다시 평가한 뒤 같은 로컬 DB에 합성 효과 1건 생성. 외부 실행 없음 |
| 승인 전까지 진행 | 같은 실행에 합성 무결성·관측 입력을 연결하고 T10이 WATCH → DONE으로 전이 |

모든 단계의 화면은 T10에 저장된 상태와 이벤트를 읽는다. 타이머가 상태 이름을 바꾸거나 클라이언트가 상태를 지정하지 않는다. 상태를 건너뛰는 SQL 변경도 없다. UI의 상단 데모 조작부를 사용하며 일반 제품 변경 요청은 이 서버에서 차단한다.

## 실제 코드와 합성 입력의 경계

| 기존 구현 재사용 | 합성 입력·대체 |
|---|---|
| `PlanStore`·`CollectionProof`·`WorkflowRecovery` | 수집 완료와 분석·AI 제안 내용 |
| 변경 승인과 적용 claim의 단일 소비 | T15 검증·T31 적용 성공 입력 |
| T19 `T5Contract`·`evaluate`·`consume` | `demo-gate` 프로필의 양쪽 시험 3개와 로컬 출처 신뢰 |
| SHIFT 승인·컷오버 승인·실행 접수·응답 유실 시 보류 | AWS·온프렘 실행 대신 같은 파일의 합성 실행 결과 |
| 계획/버전/실행 결속·WATCH 전이·SSE | 무결성·SLI·통제 재시험 결과 입력 |

데모 PASS는 3개 합성 시험에 관한 규칙 평가다. 전체 20개 통제나 운영 발급자의 서명 검증, 실제 데이터 복제·쓰기 정지·Edge 전환·무중단·복구를 검증한 것으로 해석하지 않는다. 결과에 `synthetic=true`, `operational_acceptance=false`, `authorizes_production=false`를 포함한다.

## 차단 장면

각 실행은 다른 디렉터리를 사용하며 원래 실행을 변경하지 않는다.

```sh
# 대상 시험 1개가 실패한다. 실제 T19 규칙이 BLOCK을 산출하고 GATE에 정지한다.
uv run --no-sync python -m demo_workflow.server --scenario gate-block --port 18133

# 합성 효과를 저장한 뒤 응답을 잃는다. 실제 T10이 SAFE_HOLD로 정지한다.
uv run --no-sync python -m demo_workflow.server --scenario response-loss --port 18134
```

SAFE_HOLD는 새로고침·재시작으로 해제하지 않는다. 이 데모에는 강제 PASS, 강제 완료, 승인 자동 생성, 운영 요청 재실행, 임의 계획 선택 기능이 없다.

## 검증

```sh
uv run --no-sync python -m pytest -q tests/test_demo_workflow.py tests/test_demo_workflow_server.py
uv run --no-sync ruff check demo_workflow tests/test_demo_workflow.py tests/test_demo_workflow_server.py
pnpm exec playwright test --config demo_workflow/playwright.config.mjs
```

별도 브라우저 자격 시험 서버는 18132 포트를 사용한다. 서버와 시험에는 외부 클러스터·AI·AWS 로그인이 필요 없다. 기존 콘솔 UI에 데모 모듈을 설치·활성화하지 않으면 이 경로는 생기지 않는다. 콘솔 wheel은 기존대로 `src/bravo_console`만 포함하므로 데모 서버와 자산은 배포 패키지에 포함되지 않는다.

## 인계와 운영 해피패스

UI 인계 기준 `1489a52e`는 그대로 보존한다. 이 데모는 그 커밋을 기반으로 별도 브랜치에 있으며 `src/bravo_console`과 T10/T19 제품 코드를 바꾸지 않는다. UI 담당자는 데모 상단 패널을 바꾸거나 제거해도 제품 API 계약을 변경할 필요가 없다.

사용자가 제공한 11월 6일 16:55 기준 장애 보고는 원 AI 접수 불명, T14 저장·재조회, T15 호스트 연결의 운영 인수가 끝나지 않았음을 설명한다. 이 데모는 해당 원 요청·계획·원장·복구 시도를 인수하거나 수정하지 않는다. 운영 경로 복구는 기존 담당의 원 요청에서 계속하며, 데모의 DONE을 그 요청의 복구 증거로 쓰지 않는다.
