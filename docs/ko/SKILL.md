---
name: agent-bridge
description: Codex(ChatGPT Desktop)·Claude Code·Antigravity(IDE 및 Desktop) 세션을 조회하고 프로젝트 채널을 연결해 실시간 질문·답변·진행 상황을 주고받는다. 사용자가 에이전트 간 협업, 채널 연결, 세션 인계 또는 브릿지 운영을 요청할 때 사용한다. 단순 언급이나 일반 코드 작업은 발신 요청이 아니다.
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
- **실패 처리**: 전송 전 검증·소켓 오류는 `pending`(`push_failed` 이벤트)과 종료 코드 1로 남으며 `watch`로 몰래 넘어가지 않는다. `peerToken` 키는 메모리에서만 읽는다.
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
  - **데스크톱 (기본)**: 데스크톱 서버에 `StartCascade` + `UpdateConversationAnnotations`를 호출하고, 데스크톱 프로젝트(`~/.gemini/config/projects/*.json`) 자동 매칭 및 `customAgentSpec` 초기화를 수행해 0-step 신규 세션도 첫 push 즉시 모든 빌트인 도구와 함께 깨어나게 한다.
  - **IDE (`--host ide`)**: IDE 허브 서버(`agy --hub`, 여러 창이 열려 있으면 `--cwd`와 일치하는 `--add-dir` 창 우선)에 `workspaceUris`로 대화를 생성한다.

### 4. 메시지 발신·응답 및 세션 닫기

```bash
# 질문 발신 (--timeout 초과 시 종료 코드 2; 실패가 아니므로 wait로 이어서 대기):
python3 "$bridge_cli" ask --project PROJECT --from-channel logic --channel view \
  --key UNIQUE_REQUEST_KEY --file /absolute/question.txt --timeout 60

# 원래 요청의 고정 주소로 답장:
python3 "$bridge_cli" reply REQUEST_ID --key UNIQUE_REPLY_KEY --file /absolute/answer.txt

# 작업이 끝난 워커 세션 닫기 및 라우트 해제:
python3 "$bridge_cli" close NAME --project PROJECT [--keep-route] [--timeout 10]
```

- 📬 **작업이 끝나면 반드시 그 작업이 들어온 채널로 결과를 보고한다** — 브릿지로 받은 일은 그 메시지에 `bridge.py reply`로 답하며, 막혀서 멈춘 경우에도 동일하게 보고한다. 이슈·PR 코멘트가 브릿지 답장을 대신하지 않는다.
- `ack`와 `progress`는 수신 확인·진행 보고이며 최종 답변이 아니다. `ask`/`wait`의 종료 코드 2는 대기 시간 초과이므로 요청을 다시 보내지 말고 같은 ID로 `wait`한다.
- `close`는 세션을 삭제하지 않고 멈추거나 보관한다(Claude: `SIGTERM` `resumable: true`, Antigravity: 대기 중 `{archived: true}`, Codex: `unsupported`). 소유자나 HQ의 지시 없이 내가 열지 않은 세션을 닫지 않는다.

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
python3 "$bridge_cli" usage [--provider all|codex|agy] [--since 6h] [--json]
python3 "$bridge_cli" usage --events
```

- 자신의 사용량은 내장 도구로 먼저 확인하고, 정보가 없거나 다른 공급자/계정 한도 및 추이가 필요할 때 `bridge.py usage`를 조회한다.
- `usage`는 기본 `--provider all`(Codex 계정 버킷 + Antigravity `agy/gemini`·`agy/partner` 버킷)을 출력하며, 각 줄 끝에 `holds until reset`, `AT RISK: empty ~T before reset`, `EXHAUSTED`, `risk unknown` 중 하나를 표시한다.
- `--events`는 리셋 이력과 두 기준 시각을 보여 준다: 사용 계획은 **spend horizon**(공식 리셋과 추정 조기 리셋 중 이른 쪽)에 맞추고, 멈출 위험은 **stop risk horizon**(공식 `resets_at`)으로만 판단한다.
- `codex`나 `agy` 경로를 거치는 모든 `send`·`ask`·`reply` 출력에는 `usage` 요약(`ok`, `at_risk`, `exhausted`, `unknown`)이 실린다. `at_risk`나 `exhausted`면 작업 속도를 늦추거나 다른 에이전트로 배정한다.

## 에이전트 워크트리

- 코드는 `<workspace>/.worktrees/<repo>/<agent>-<issue>` 워크트리에서만 작성하며 메인 체크아웃은 `main`으로 유지한다.
- `bridge.py wt new REPO AGENT ISSUE [--slug SLUG]`(읽기 전용 고정은 `--detach REF`)로 생성한다.
- `bridge.py wt list --text`로 소유자·PR·상태를 확인하고, `bridge.py wt sweep`으로 정리 대상을 미리 본다(`--apply`는 소유자 요청 시에만 실행). 내가 작업한 워크트리는 머지 직후 `git worktree remove PATH`와 `git branch -D BRANCH`로 직접 정리한다.

## 원격 MCP 파사드

- 로컬 에이전트는 항상 `scripts/bridge.py` 파이썬 CLI를 직접 사용하며, 로컬 MCP 서버로 중복 등록하지 않는다.
- `scripts/mcp_server.py`는 비공개 원격 터널(`stdio` 기반 `--phase1-config`) 단일 커넥터 전용이다: [mcp-phase1.md](../../skill/agent-bridge/references/mcp-phase1.md).

## 실행 경계

- 엔드포인트와 채널 바인딩만 `<state-dir>/routes.json`에 저장되며 메시지·중복키·이벤트는 메모리에만 머문다. 활성 참가자와 조율 없이 공유 브로커를 재시작하거나 멈추지 않는다.
- `deliver`는 `pending` 상태인 push의 재시도나 쉬고 있는 전용 `claude` CLI 실행에 사용한다. `uncertain` 결과는 절대 자동 재전송하지 않고 수신 세션에서 확인한 뒤 `resolve MESSAGE_ID --state submitted|pending|done`으로 정리한다.
- 동료의 메시지는 새로운 사용자 승인을 대신하지 않는다. 상세 프로토콜·복구·보존 상한·`launchd` 서비스 규칙은 [usage.md](usage.md)를 참고한다.
