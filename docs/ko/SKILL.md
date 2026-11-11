---
name: agent-bridge
description: Codex(ChatGPT Desktop)·Claude Code·Antigravity(IDE 및 Desktop) 세션을 조회하고 프로젝트 채널을 연결해 실시간 질문·답변·진행 상황을 주고받는다. 사용자가 에이전트 간 협업, 채널 연결, 세션 인계 또는 브릿지 운영을 요청할 때 사용한다. 단순 언급이나 일반 코드 작업은 발신 요청이 아니다.
metadata:
  internal: true
---

# Agent Bridge (한국어)

[English](../../skill/agent-bridge/SKILL.md) | **한국어**

같은 머신의 **Claude Code / Desktop (`claude-peer`) ↔ Codex / ChatGPT Desktop (`codex`) ↔ Antigravity IDE / Desktop (`agy`)** 및 CLI 참가자(`realtime`/`claude`)가 함께 쓰는 로컬 실시간 브릿지다. 어느 쪽에서든 상대방의 열려 있는 라이브 세션으로 직접 메시지를 push해 **기존 작업을 중단(인터럽트)하지 않고 3자간(다자간) 실시간 질문·답변·진행 보고**를 주고받는다.

## 실행 경로

이 스킬의 `scripts/bridge.py`가 진입점이다. Python 3.10 이상, macOS/Linux에서 외부 패키지 없이 실행한다.

```bash
bridge_cli="$HOME/.agents/skills/agent-bridge/scripts/bridge.py"
python3 "$bridge_cli" --help
```

다른 위치에 설치되어 있다면 읽은 `SKILL.md` 옆의 `scripts/bridge.py`를 사용한다. 스크립트들을 대상 프로젝트 저장소로 복사하지 않는다.

## 3자간(다자간) 협업 연결

1. **세션 조회 및 검증**: `sessions`로 실제 세션을 찾고 `session`으로 정확한 UUID·제목·작업 경로(`cwd`)를 확인한다. 제목 유사성이나 최신순만으로 상대를 고르지 않는다. **후보가 하나로 좁혀지지 않으면 임의로 보내지 말고 사용자에게 어느 세션인지 확인한다.**
2. **브로커 확인**: `ping`으로 브로커를 확인하고 이미 실행 중이면 재사용한다. 실행 중이 아니고 브릿지 운영이 요청 범위라면 `serve`를 실행해 `ready`를 확인한다.
3. **라우트 및 채널 등록**: 등록 전에 `routes`와 `channels`를 확인해 다른 세션의 연결을 덮어쓰지 않는다. 라이브 세션은 `claude-peer`, `codex`, `agy`로 등록하고 `channel-bind`로 역할 채널을 연결한다.
   `bridge.py remap`은 브로커의 모든 프로젝트에서 보관되지 않은 등록 Codex 세션을 다시 연다. 저장된 작업 디렉터리를 확인하고 Desktop 소유자가 이미 붙은 세션은 건너뛴다. 결과는 엔드포인트별로 출력한다. 세션을 다시 열 때 Codex Desktop이 화면 앞으로 올 수 있다. UUID와 대기 중인 메시지는 그대로 유지한다.

   `bridge.py remap NAME --project PROJECT --expect-session OLD_UUID --session NEW_UUID`로 등록된 Codex 엔드포인트의 UUID를 교체할 수 있다. 새 세션에는 라이브 데스크톱 소유자가 있어야 하며 작업 디렉터리는 기존과 같아야 한다. 참가자 이름으로 직접 보내는 새 메시지에 교체된 UUID를 사용한다. 채널 바인딩과 기존 메시지는 원래 라우트를 유지한다.

4. **직접 Push (폴링 루프 금지)**: `watch`는 `realtime` CLI 워커 전용이다. **`claude-peer`, `codex`, `agy`는 별도의 `watch` 폴링 루프나 새 CLI 프로세스 없이 기존 라이브 세션에 직접 push한다.**

```bash
python3 "$bridge_cli" sessions --cwd /absolute/project
python3 "$bridge_cli" session claude SESSION_UUID
python3 "$bridge_cli" session codex SESSION_UUID
python3 "$bridge_cli" session agy SESSION_UUID
python3 "$bridge_cli" ping
# 브로커가 없을 때만 실행:
python3 "$bridge_cli" serve
```

### 1. Claude Code / Desktop (`claude-peer`)

```bash
# 기존 Claude 라이브 세션 등록:
python3 "$bridge_cli" register claude-agent --project PROJECT --transport claude-peer \
  --session CLAUDE_SESSION_ID --discover

# Claude Code IDE 새 세션 무인 열기 (--host ide):
python3 "$bridge_cli" register claude-worker --project PROJECT --transport claude-peer \
  --new --host ide [--ide antigravity-ide|vscode|cursor] --cwd /path/to/workspace --timeout 120

# Claude 데스크톱 새 세션 열기 (입력창 미리 채움 · 사용자의 Enter 대기):
python3 "$bridge_cli" register claude-worker --project PROJECT --transport claude-peer \
  --new --cwd /path/to/workspace --title "새 세션의 첫 메시지" --timeout 300
```

- **직접 Push**: `--discover`는 `~/.claude/sessions/<pid>.json`에서 정확한 `sessionId`, 생존 여부, `peerProtocol == 1`, 작업 경로를 검증한다. `send`·`ask`·`reply`는 즉시 메시징 소켓으로 주입(`submitted`)되며, 마지막 줄에 브릿지 답장 명령(`bridge.py reply <ID> …`)이 실린다.
- **실패 처리**. 쓰기 전 오류는 `push_failed` 이벤트와 함께 `pending`으로 남는다. 쓰기가 시작된 뒤 쓰기 또는 연결 종료 대기에 실패하면 `uncertain`으로 기록한다. `deliver ID --timeout SECONDS`는 해당 전송의 조회, claim, peer I/O에 적용된다. 쓰기가 시작됐을 수 있다면 브로커에 결과를 기록하는 데 최대 10초를 더 쓸 수 있다. peer에 쓰기 전 보호된 claim 응답을 잃으면 접수 한도가 지날 때까지 기다렸다가 자기 예약을 푸는 데 최대 2초를 더 쓴다. 해제가 확인되면 `push_failed` 이벤트와 `pending`을 남긴다. claim, 해제, finish 응답이 미확정이면 메시지 ID와 단계를 출력한다. `resolve` 전에 `show ID`로 상태를 확인한다.
- **대기 중인 전송**. `show`와 CLI `inbox`는 다른 전송에 막힌 `pending` 메시지에 `blocked_by`와 `pending behind <id> since <time>`을 표시한다. `dispatching` 전송(깨우기 등) 뒤의 `claude-peer` push는 자기 한도 안에서 그 전송을 한 번 기다린다. 그 전송이 전달로 끝나면 한 번 push하고, 아니면 앞 전송이 끝난 상태를 붙인 그 노트(`… (<상태>); not sent`)를 남긴다. 차단 중인 전송을 `resolve`하기 전에 발신 측 전송 클라이언트가 종료됐는지와 정확한 수신 세션을 확인한다. 미확정 전송은 자동 재시도하지 않는다. `peerToken` 키는 메모리에서만 읽는다.
- **유휴로 닫힌 세션 (wake)**: Claude 데스크톱은 안 보이는 세션을 마지막 턴 뒤 900초에 멈추고, 메모리가 부족하면 쉬는 세션을 내린다. 그러면 기록과 소켓이 사라진다. 그 라우트로 push하면 먼저 `claude://claude.ai/epitaxy/<local_id>`를 열고, 같은 `sessionId`가 inbox 소켓과 함께 돌아오기를 기다린 뒤 한 번 push한다(최대 30초, 전송 한도 안에서, push용 5초를 남긴다)(결과에 `wake: woken`). `unsupported`나 `uncertain`이면 아무것도 보내지 않은 채 `pending`으로 돌아간다. 이걸 반복해서 돌리지 않는다. 명시적으로는 `wake NAME --project P`로 확인한다(`woken` · `already-live`는 종료 코드 0, `unsupported` · `uncertain`은 1). 링크를 열면 앱이 그 세션으로 포커스를 옮기고 앞으로 올 수 있다. 예약 작업 세션과 IDE 세션은 `unsupported`다.
- **새 세션 열기**:
  - **IDE (`--host ide`)**: IDE CLI로 해당 폴더 창을 포커스/오픈한 뒤 `<scheme>://anthropic.claude-code/open`을 열어 `--cwd` 아래 새 `entrypoint == "claude-vscode"` 레코드 하나를 사람 개입 없이 즉시 등록한다.
  - **데스크톱 (기본)**: `claude://code/new?folder=…&q=…`를 열고 사람이 Enter를 눌러 생성된 `entrypoint == "claude-desktop"`(`hostSessionId: "local_…"`) 레코드를 `--timeout`까지 기다려 등록한다.
  - 새 Claude 세션 생성 시 `--model`은 거부된다(UI 선택기 또는 데스크톱 `model --set` 사용).
- ⛔ **`--transport claude`(resume)는 라이브 세션에 절대 쓰지 않는다.** 살아 있는 대화에 들어가지 않고 별개의 CLI 사본을 띄운다. 쉬고 있는 전용 CLI 세션에만 사용한다.

### 2. Codex / ChatGPT Desktop (`codex`)

```bash
# 기존 Codex 데스크톱 라이브 세션 등록:
python3 "$bridge_cli" register codex-agent --project PROJECT --transport codex \
  --session CODEX_SESSION_UUID --discover

# 새 Codex 데스크톱 세션 무인 생성 및 인계:
python3 "$bridge_cli" open --provider codex --cwd /path/to/workspace --title "작업 세션" --model MODEL
python3 "$bridge_cli" register codex-worker --project PROJECT --transport codex \
  --new --cwd /path/to/workspace --title "작업 세션" --model MODEL --timeout 180
```

- **직접 Push**: `CODEX_HOME/ipc/ipc.sock`(기본 `~/.codex`, 또는 `AGENT_BRIDGE_CODEX_IPC`)에 연결해 `supportsUntrustedAppInput`을 지원하는 정확한 대화 소유자를 확인한다. 동료 본문은 standalone `toolOutput`(`agent_bridge`)로 전달하며, 활성 턴이면 `thread-follower-steer-turn`으로 합류하고 유휴 상태일 때만 `thread-follower-start-turn`을 호출한다.
- **사이드바 미리보기**: 첫 브릿지 과제가 `submitted`로 접수된 뒤 비어 있는 네이티브 사이드바 `preview`를 `state_5.sqlite`에 한 번 초기화하며, 실패하더라도 이미 수락된 전달을 실패로 바꾸지 않는다.
- **무인 세션 생성 (`open` / `register --new`)**:
  - `app-server --stdio`로 영속 대화를 만들고 `thread/name/set`으로 제목을 저장한 뒤 `codex://threads/UUID`를 열어 데스크톱 앱이 대화를 인계받게 한다. **Enter 입력이나 UI 자동화가 필요 없다.**
  - 모델 정책 및 재조회 검증(`--model` > `AGENT_BRIDGE_CODEX_MODEL` > `policy.json` `defaults.codex` > `gpt-6.1-sol`; `gpt-6-astra`로 코딩 세션을 열지 않음)과 Full Access 권한(`:danger-full-access`, `approvalPolicy: never`)을 강제한다.
  - 일반 `open --provider codex --title TITLE`은 동일한 제목·`cwd`의 보관되지 않은 기존 스레드가 있으면 중복 생성을 거부(`stage: "duplicate"`)하며, `--new`를 주면 중복 검사를 건너뛴다. 실패하거나 `uncertain`인 생성을 자동 재실행하지 않는다.

Codex 공급자 실패는 등록된 모든 Codex 스레드를 대상으로 관찰하며 관찰 회차 사이 기본 간격은 10초다. `serve --codex-failure-interval SECONDS`로 주기를 정하며 `0`은 관찰을 끈다. 등록된 스레드·현재 Desktop 소유자·최신 최종 턴를 고정하고, 실행 상태가 idle이며 최신 턴의 완료 상태가 `failed`이고 일치하는 최종 오류가 `willRetry: false`일 때 알려진 네트워크·스트림 실패를 감지한다. 원래 메시지의 고정 반환 주소로 정규화한 `provider_failure_detected` 알림 하나를 남긴다. 소유자의 `inbox`에서 ID를 찾고 `show ID`로 읽거나 MCP inbox를 사용한다. 원본 작업 상태는 유지하며 완료된 도구 호출이 있는 턴도 감지한다.

알림에는 `observed_at`·턴 ID·실패 종류를 담는다. `failed_at`은 턴 시작 시각과 실행 시간이 모두 있으면 합산하고 `failure_time_source: turn_duration`을 표시하며, 자료가 없으면 null이다. 공급자 원문·도구 출력·Desktop 소유자 메타데이터는 알림과 체크포인트에서 제외한다. 앱의 capacity 재시도를 따른다. 브로커 재시작 뒤 등록 라우트에서 관찰을 재개한다. 진행 중 작업이 없어도 해당 라우트의 마지막 보관 발신자에게 알린다. 보관 한도 내에 발신자가 없으면 등록 스레드 자신의 inbox에 알림을 남긴다. 보관 한도 내 알림의 중복 방지는 재시작 후에도 유지한다. 소유자 부재·미지원 스냅샷·무응답 시간만으로 공급자 실패를 판정하지 않는다.

Codex 복구는 네이티브 Resume와 같은 기존 스레드 ID·빈 `input`·`turnTrigger: "resume_interrupted_task"`·상속된 스레드 설정을 사용한다. 브릿지에는 composer가 없으므로 `additionalContext`를 생략하고 writing-block 문맥 준비를 표시해 빈 composer의 Resume와 맞춘다. 기본 최소 대기는 시도별 15·30·60초이며 수락된 시도를 최대 3회 센다. `serve --provider-retry-backoff 15,30,60 --provider-retry-cap 3`으로 바꾼다. 짧은 대기 목록은 마지막 값을 재사용한다. 관찰 주기만큼 대기가 추가될 수 있으며 정책은 각 실패 묶음을 시작할 때 고정한다.

전송 직전에 고정 소유자·최신 실패 턴·입력 fingerprint·idle 상태·최종 `willRetry: false`·미결 요청·도구 완료를 다시 확인한다. 완료된 도구 호출은 허용한다. 더 최신 턴·중간 입력·소유자 변경·출력 없는 시작 호출이 있으면 `provider_retry_unsafe`로 멈춘다. IPC 전에 시도를 영구 예약하며 원래 감지 알림과 같은 대화에 `provider_retry` k/N 메시지를 남긴다. 수락된 시도만 세고 마지막 시도까지 실패하면 `provider_retry_exhausted`를 알린다. 전송 결과가 모호하거나 재시작 시 미결 예약이 있으면 `provider_retry_uncertain`으로 멈추고 재전송하지 않는다. 이후 턴이 completed 또는 interrupted로 끝나면 횟수를 초기화한다. 같은 Codex 스레드의 별칭도 횟수를 공유한다. 정규화한 예약·횟수·fingerprint만 비공개 correspondence 메타데이터로 저장하고 알림·이벤트 본문은 보존한다. 초기화 전까지 활성 실패 묶음의 횟수는 보관 한도로 삭제하지 않는다. correspondence 체크포인트 유실이 확인되면 `provider_retry_uncertain`으로 멈춘다.

### 3. Antigravity IDE & Desktop (`agy`)

```bash
# 열려 있는 Antigravity IDE · 데스크톱 세션 조회 및 등록:
python3 "$bridge_cli" sessions --provider agy --cwd /path/to/workspace
python3 "$bridge_cli" register agy-agent --project PROJECT --transport agy \
  --session AGY_CONVERSATION_ID --discover

# 새 Antigravity 데스크톱 또는 IDE (--host ide) 세션 무인 생성:
python3 "$bridge_cli" open --provider agy [--host ide] --cwd /path/to/workspace --title "Worker"
python3 "$bridge_cli" register agy-worker --project PROJECT --transport agy \
  --new [--host ide] --cwd /path/to/workspace --title "Worker"
```

- **직접 Push**: 로컬 Antigravity `language_server` / `agy --hub`의 ConnectRPC에 연결해 VS Code 익스텐션 없이 **Antigravity IDE 창과 Agent Manager(Desktop) 창의 모든 라이브 세션**을 함께 커버한다. 동일 대화가 여러 서버에 캐시되어 있을 때는 실행 중(`running`)이거나 가장 최신(`stepCount`, `lastModifiedTime`, `--add-dir` 워크스페이스 친화도)인 서버를 선택하며, 유휴 고아 `agy --hub` 프로세스(`ppid == 1`)는 자동 정리한다.
- **합류 및 깨우기**: 대화의 메시지 허브(`SendAgentMessage`, `MESSAGE_PRIORITY_HIGH`)로 직접 주입해 활성 턴(`running`)이면 기존 작업을 끊지 않고 다음 스텝에 `<SYSTEM_MESSAGE>[Message] ...`로 합류하고, 유휴/백그라운드 대기 상태면 빈 `SendUserCascadeMessage`(`NEXT_INVOCATION`)로 즉시 깨운다. AGY 내부 `send_message` 도구는 서브에이전트 전용이므로 반드시 셸에서 `bridge.py reply`로 답한다.
- **무인 세션 생성 (`open` / `register --new`)**:
  - **데스크톱과 IDE (`--host desktop` / `--host ide`) 동일 동작**: 통합 Antigravity 서버(`~/.gemini/antigravity`)에 `StartCascade` + `UpdateConversationAnnotations`를 호출하고, 프로젝트(`~/.gemini/projects/*.json`) 및 워크트리 환경(`projectEnvConfig`) 자동 매칭·생성 및 `customAgentSpec` 초기화를 수행해 0-step 신규 세션도 첫 push 즉시 모든 빌트인 도구와 함께 깨어나며 대화 스택에 동일하게 쌓인다.

Antigravity (`agy`) 공급자 실패 관찰은 등록된 모든 `agy` 라우트를 대상으로 기본 10초마다 서버당 `GetAllCascadeTrajectories`를 1회 폴링하며(`serve --agy-failure-interval SECONDS`, `0`은 비활성화), 세션이 `IDLE`이고 `stepCount`가 바뀌었을 때만 마지막 스텝(`GetCascadeTrajectorySteps`, `stepOffset = stepCount - 1`)을 조회한다. 내부 재시도(`1–8`차, `RUNNING` 상태이거나 `shouldShowUser: true`가 없는 스텝)는 무시한다. 최종 사용자 노출 오류(`shouldShowUser: true`, `CORTEX_STEP_STATUS_DONE`, 뒤따르는 스텝 없음)는 `(sid, step_index, errorId)`로 중복을 방지하고 `agy:<step_index>` 상관 ID를 사용한다. `WAKE_ELIGIBLE_ERROR_SUBSTRINGS`(`"http2: timeout awaiting response headers"`)와 일치하는 최종 스텝만 공통 `15/30/60`초·최대 `3`회 정책(`provider_retry` k/N, `provider_retry_exhausted`, `provider_retry_unsafe`, `provider_retry_uncertain`)으로 예약 후 `wake_session`(빈 `items`의 `SendUserCascadeMessage`, `NEXT_INVOCATION`)으로 깨우며, 그 외 최종 오류 스텝은 `provider_failure_detected`만 기록하고 깨우지 않는다. 진행 중 메시지 유무와 관계없이 알림은 해당 라우트의 마지막 보관 발신자(없으면 라우트 자신의 inbox)로 전달된다. 깨운 뒤 다음 폴링 주기에 `stepCount`가 늘지 않고 `RUNNING`도 아니면 `provider_retry_uncertain`을 기록하고 다시 깨우지 않는다. 메모리 내 스텝 캐시는 브로커 재시작 시 초기화된다.

### 4. 메시지 발신·응답 및 세션 닫기

```bash
# 질문 발신 (--timeout 초과 시 종료 코드 2; 실패가 아니므로 wait로 이어서 대기):
python3 "$bridge_cli" ask --project PROJECT --from-channel logic --channel view \
  --key UNIQUE_REQUEST_KEY --file /absolute/question.txt --timeout 60

# 원래 요청의 고정 주소로 답장:
python3 "$bridge_cli" reply REQUEST_ID --key UNIQUE_REPLY_KEY --file /absolute/answer.txt

# 작업이 끝난 워커 세션 닫기 및 라우트 해제 (사이드바에서 보관 처리하려면 --archive 추가):
python3 "$bridge_cli" close NAME --project PROJECT [--archive] [--keep-route] [--timeout 10]
```

- 📬 **작업이 끝나면 반드시 그 작업이 들어온 채널로 결과를 보고한다** — 브릿지로 받은 일은 그 메시지에 `bridge.py reply`로 답하며, 막혀서 멈춘 경우에도 동일하게 보고한다. 이슈·PR 코멘트가 브릿지 답장을 대신하지 않는다.
- `ack`와 `progress`는 수신 확인·진행 보고이며 최종 답변이 아니다. `ask`/`wait`의 종료 코드 2는 대기 시간 초과이므로 요청을 다시 보내지 말고 같은 ID로 `wait`한다.
- `close`는 기본적으로 세 공급자 모두 세션을 삭제하거나 아카이빙하지 않고 대화 스택에 그대로 남긴 채(`archived: false`, `resumable: true`) 세션/라우트를 종료하며(Claude: 백그라운드 CLI 프로세스에 `SIGTERM` 전송, Antigravity: 대기 상태 확인 후 분리, Codex: 스레드 분리 및 라우트 제거), `--archive` 옵션을 주면 사이드바에서 보관 처리(`archived: true`)까지 함께 수행한다. 소유자나 HQ의 지시 없이 내가 열지 않은 세션을 닫지 않는다.

## 모델: 읽기 · 전환 · 정책

```bash
python3 "$bridge_cli" model NAME --project PROJECT              # 조회 시점에 새로 읽기
python3 "$bridge_cli" model NAME --project PROJECT --set MODEL  # 전환 후 재조회 검증
python3 "$bridge_cli" routes --project PROJECT                  # 경로마다 "model" 포함 (--no-model로 생략)
python3 "$bridge_cli" policy                                    # 적용 중인 기본값 · 거부 목록
```

- **정책**: `<state-dir>/policy.json`(예: `{"defaults": {"agy": "gemini-4-argon-medium"}, "deny": {"codex": ["gpt-6-astra"]}}`)에 저장하며 사람만 수정한다. 파일이 없을 때는 시드 거부 목록 `{"codex": ["gpt-6-astra"]}`가 적용된다.
- **Antigravity (`agy`)**: 마지막 generator의 `plannerConfig`를 읽는다. 전환은 대기 중일 때만 허용되며, 기존 플래너 설정을 유지한 채 `requestedModel`만 바꿔 **새 모델로 실제 턴 1회**를 실행한다(실측 generator 2–8개, 10–90초; `generators_spent`·`busy_after` 보고). `busy_after: true`면 전환은 완료된 것이므로 다시 보내지 말고 대기 상태가 될 때까지 기다린다.
- **Codex (`codex`)**: 정확한 데스크톱 소유자의 **다음 턴 모델 설정**을 읽고 `thread-follower-update-thread-settings`로 전환 후 재조회로 검증한다. 진행 중인 턴은 끊지 않는다.
- **Claude (`claude-peer`)**: 대화 기록의 최신 assistant 메시지에서 모델을 읽는다. 데스크톱 세션 전환은 `policy.json`에 지정된 `command_post` 세션(`{"command_post": {"claude": "<경로>"}}`)을 통해 `set_session_model`을 중계하며, 전환 응답(`applies: next turn`)이 확정 결과다(한 턴이 돌기 전까지 기록 조회에 이전 모델이 보여도 재전환하지 않는다). VS Code / IDE 세션 전환은 `unsupported (STOP)`다.

## 사용량 확인

```bash
python3 "$bridge_cli" usage [--provider all|codex|agy|claude] [--since 6h] [--refresh] [--json]
python3 "$bridge_cli" usage --events
```

- 자신의 사용량은 내장 도구로 먼저 확인하고, 정보가 없거나 다른 공급자/계정 한도 및 추이가 필요할 때 `bridge.py usage`를 조회한다.
- `usage`는 기본 `--provider all`(Codex 계정 버킷 + Antigravity `agy/gemini-weekly`·`agy/gemini-5h`·`agy/partner-weekly`·`agy/partner-5h` 버킷 + Claude `claude/desktop`·`claude/ide` 버킷)을 출력하며, 용량 판정 대상 줄 끝에 `holds until reset`, `AT RISK: empty ~T before reset`, `EXHAUSTED`, `risk unknown` 중 하나를 표시한다.
- Antigravity (`agy`): `RetrieveUserQuotaSummary`를 조회한다(기본 `{}` 호출로 서버의 5분 캐시를 사용하며 백그라운드 수집기는 절대 `forceRefresh`를 보내지 않고, 명시적 `--refresh`만 `{"forceRefresh": true}`를 전송한다). `3p-*` 버킷은 `partner-*`로 기록하고 기존 `agy/gemini`·`agy/partner` 레코드도 히스토리로 유지한다. 주간 수치가 없으면 `weekly: unknown`(`0%`나 `100%` 금지)으로 표시하며, `AT RISK` / `holds until reset` 판정은 주간 버킷(`*-weekly`)에서만 수행하고 5시간 창은 표시만 한다.
- `--events`는 리셋 이력과 두 기준 시각을 보여 준다: 사용 계획은 **spend horizon**(공식 리셋과 추정 조기 리셋 중 이른 쪽)에 맞추고, 멈출 위험은 **stop risk horizon**(공식 `resets_at`)으로만 판단한다.
- Claude: `claude/desktop`은 Claude 데스크톱 계정(데스크톱 자체 기록, 사용률만 · 리셋 시각 없음), `claude/ide`는 IDE 확장 · CLI가 쓰는 Claude Code 로그인(키체인 토큰으로 사용량 엔드포인트 실시간 조회, 안 되면 `~/.claude.json` 캐시)이다. 데스크톱 HQ는 여기서 IDE 세션의 한도를, IDE 세션은 데스크톱의 한도를 본다. 어느 읽기도 모델 턴을 시작하지 않는다. 소모율과 위험 판정은 가장 최근 읽기가 본 계정(불투명한 해시)의 표본만 쓰고, 다른 계정의 표본은 섞지 않는다. Claude Code 세션 안에서 `usage`는 경고 한 줄을 먼저 찍는다 — 자기 Claude 한도는 브릿지가 아니라 앱(`get_usage`)에서 읽는다.
- `codex`나 `agy` 경로(또는 `claude-peer`끼리의 경로)를 거치는 모든 `send`·`ask`·`reply` 출력에는 `usage` 요약(`ok`, `at_risk`, `exhausted`, `unknown`)이 실린다. `at_risk`나 `exhausted`면 작업 속도를 늦추거나 다른 에이전트로 배정한다.

## 에이전트 워크트리

- 코드는 `<workspace>/.worktrees/<repo>/<agent>-<issue>` 워크트리에서만 작성하며 메인 체크아웃은 `main`으로 유지한다.
- `bridge.py wt new REPO AGENT ISSUE [--slug SLUG]`(읽기 전용 고정은 `--detach REF`)로 생성한다.
- `bridge.py wt list --text`로 소유자·PR·상태를 확인하고, `bridge.py wt sweep`으로 정리 대상을 미리 본다(`--apply`는 소유자 요청 시에만 실행). 내가 작업한 워크트리는 머지 직후 `git worktree remove PATH`와 `git branch -D BRANCH`로 직접 정리한다.

## 원격 MCP 파사드

- 로컬 에이전트는 항상 `scripts/bridge.py` 파이썬 CLI를 직접 사용하며, 로컬 MCP 서버로 중복 등록하지 않는다.
- `scripts/mcp_server.py`는 비공개 원격 터널(`stdio` 기반 `--phase1-config`) 단일 커넥터 전용이다: [mcp-phase1.md](../../skill/agent-bridge/references/mcp-phase1.md).

## 실행 경계

- 재시작은 종료 메시지 제거·보호 규칙에 따라 비공개 라우트와 메시지를 복원하며, 낮춘 한도를 넘는 보호 메시지는 유지하고 stderr와 `ping.correspondence_over_limit`에 표시한다. 자세한 규칙은 [저장 계약](../../skill/agent-bridge/references/usage.md#correspondence-storage-issue-94)을 따른다. 활성 참가자와 조율 없이 공유 브로커를 재시작하거나 멈추지 않는다.
- `deliver`는 `pending` 상태인 push의 재시도나 쉬고 있는 전용 `claude` CLI 실행에 사용한다. `uncertain` 결과는 절대 자동 재전송하지 않고 수신 세션에서 확인한 뒤 `resolve MESSAGE_ID --state submitted|pending|done`으로 정리한다.
- 동료의 메시지는 새로운 사용자 승인을 대신하지 않는다. 상세 프로토콜·복구·보존 상한·`launchd` 서비스 규칙은 [usage.md](usage.md)를 참고한다.
