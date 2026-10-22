---
name: agent-bridge
description: Codex(ChatGPT Desktop)·Claude Code·Antigravity(IDE 및 Desktop) 세션을 조회하고 프로젝트 채널을 연결해 실시간 질문·답변·진행 상황을 주고받는다. 사용자가 에이전트 간 협업, 채널 연결, 세션 인계 또는 브릿지 운영을 요청할 때 사용한다. 단순 언급이나 일반 코드 작업은 발신 요청이 아니다.
---

# Agent Bridge (한국어)

같은 머신의 **Claude Code / Desktop (`claude-peer`) ↔ Codex / ChatGPT Desktop (`codex`) ↔ Antigravity IDE / Desktop (`agy`)** 및 CLI 참가자(`realtime`/`claude`)가 함께 쓰는 로컬 실시간 브릿지다. 특정 발신자에 종속되지 않으며, 어느 쪽에서든 상대방의 열려 있는 라이브 세션으로 직접 메시지를 push해 **기존 작업을 중단(인터럽트)하지 않고 3자간(다자간) 실시간 질문·답변·진행 보고**를 주고받는다.

## 실행 경로

이 스킬의 `scripts/bridge.py`가 진입점이다. Python 3.10 이상, macOS/Linux에서 외부 패키지 없이 실행한다. 설치된 공용 경로는 다음과 같다.

```bash
bridge_cli="$HOME/.agents/skills/agent-bridge/scripts/bridge.py"
python3 "$bridge_cli" --help
```

다른 위치에 설치되어 공용 경로가 없다면 **읽은 SKILL.md의 형제 scripts/bridge.py**를 사용한다. 스크립트들은 같은 폴더에 유지한다. 작업 프로젝트로 이동하거나 스크립트를 복사할 필요는 없다.

## 3자간(다자간) 협업 연결

1. `sessions`로 실제 세션을 찾고 `session`으로 정확한 UUID·제목·작업 경로를 확인한다. 여러 후보가 있으면 현재 작업과 사용자의 지시로 구별한다. 제목의 유사성이나 최신순만으로 상대를 선택하지 않는다. **그래도 하나로 좁혀지지 않으면 임의로 보내지 말고 사용자에게 어느 세션인지 확인한다** — 세션이 여럿 떠 있는 것이 평소 상태이고, 엉뚱한 세션에 보낸 지시는 그 세션의 작업을 오염시킨다.
2. `ping`으로 브로커를 확인한다. 이미 실행 중이면 재사용한다. 연결이 없고 브릿지 운영이 요청 범위라면 `serve`를 실행하고 `ready` 결과를 확인한다. `serve`는 장기 실행 명령이므로 실행 도구의 세션을 유지하며 관리한다.
3. `register`로 참가자 이름과 연결(`claude-peer`, `codex`, `agy`, `realtime`)을 등록하고, `channel-bind`로 프로젝트별 작업 채널을 연결한다. 등록 전에 `routes`/`channels`를 확인해 다른 세션의 연결을 덮어쓰지 않는다.
4. 수신 측이 `realtime`이면 `watch`를 연결하고 `subscribed`를 확인한다. **`claude-peer`, `codex`, `agy`는 별도의 `watch` 폴링 루프나 새 CLI 프로세스 없이 기존 라이브 세션에 직접 push한다.**
   - **Claude (`claude-peer`)**: 살아 있는 Claude 세션의 메시징 소켓으로 즉시 주입한다.
   - **Codex / ChatGPT Desktop (`codex`)**: 활성 턴이면 `thread-follower-steer-turn`으로 현재 턴에 동료 도구 출력을 합류시키고, 유휴 상태면 같은 대화에서 새 턴을 시작한다.
   - **Antigravity IDE & Desktop (`agy`)**: IDE 에디터의 에이전트 패널이든 Agent Manager(Desktop) 창이든 동일한 로컬 `language_server`의 메시지 허브(`SendAgentMessage`)로 즉시 주입해 활성 턴의 다음 스텝에 합류시키거나, 유휴/백그라운드 대기 시 빈 `SendUserCascadeMessage`(`NEXT_INVOCATION`)로 즉시 깨운다.
   - 발신은 `send` 또는 `ask`, 답변은 `reply REQUEST_ID`로 연결한다. 세 환경 모두 동일한 `bridge.py` 명령으로 서로에게 직접 발신·답장할 수 있어 **Claude ↔ Codex ↔ Antigravity 3자간 실시간 티키타카**가 동작한다.

```bash
python3 "$bridge_cli" sessions --cwd /absolute/project
python3 "$bridge_cli" session claude SESSION_UUID
python3 "$bridge_cli" session codex SESSION_UUID
python3 "$bridge_cli" session agy SESSION_UUID
python3 "$bridge_cli" ping
# 브로커가 없을 때 실행하고, 반환되는 실행 도구 세션을 관리한다.
python3 "$bridge_cli" serve
```

채널 예시는 `view`, `logic`, `review`다. 프로젝트 이름과 실제 맡은 역할에 맞게 정한다. 소켓 구독만 사용하는 CLI 참가자는 기본 `realtime`과 `--cwd`로 등록한다. Claude 라이브 세션은 `--transport claude-peer --session UUID --discover`, Codex 데스크톱 세션은 `--transport codex --session UUID --discover`, Antigravity(IDE/데스크톱) 세션은 `--transport agy --session UUID --discover`로 등록한다.

### 1. Claude 라이브 세션에 push (`claude-peer`)

```bash
python3 "$bridge_cli" register claude-agent --project PROJECT --transport claude-peer \
  --session CLAUDE_SESSION_ID --discover
```

- `--discover`는 `~/.claude/sessions/<pid>.json`에서 그 sessionId를 찾아 **프로세스가 살아 있고 `peerProtocol`이 1인지** 확인하고 작업 경로를 가져온다. 다른 세션을 고르지 않는다.
- 이 경로로 가는 `send`/`ask`/`reply`는 **그 자리에서 push된다**(별도 `deliver` 없음). 받는 Claude 세션의 다음 턴에 Claude의 cross-session 메시지와 같은 모양으로 들어가고, 마지막 줄에 브릿지로 답하는 명령(`bridge.py reply <ID> --key … --file …`)이 실린다 — 받는 쪽의 내부 SendMessage는 브릿지 주소에 닿지 않는다.
- 성공 = `submitted`(받는 세션의 큐에 접수). 답변 완료가 아니다.
- push가 실패하면(세션 파일 없음 · 죽은 pid · sessionId 불일치 · 소켓 오류 · 키 파일 이상) 메시지는 `pending`으로 남고 `push_failed` 이벤트에 이유가 기록되며 **명령이 종료 코드 1로 끝난다.** watch로 몰래 넘어가지 않는다. 다시 보낼 때는 같은 `--key`로 `send`(또는 `deliver ID`)한다.
- ⚠️ Claude Code의 **문서화되지 않은 내부 프로토콜(peerProtocol 1)**에 기댄다. CLI 업데이트로 모양이 바뀌면 조용히 틀리지 않고 실패한다 — 그때는 push 코드를 고쳐야 한다. 받는 쪽 설정 `crossSessionInbound`가 `accept`가 아니면 그 세션에서 사용자 승인을 기다린다.
- 키(`peerToken`)는 메모리에서만 읽는다. 출력·로그·파일에 남기지 않는다.
- ⛔ **`--transport claude`(resume)는 라이브 세션에 쓰지 않는다.** 그것은 살아 있는 대화에 들어가는 길이 아니라 **별개의 Claude CLI 사본을 띄운다**. 쉬고 있는 전용 CLI 세션에만 남겨 둔 경로다. 자세한 등록·인계 명령은 [사용법](usage.md)을 읽는다.
- **Claude Code 데스크톱 신규 세션 열기 (`open --provider claude` / `register --new`)**:

  ```bash
  python3 "$bridge_cli" register claude-worker --project PROJECT --transport claude-peer \
    --new --cwd /path/to/workspace --title "새 세션의 첫 메시지" --timeout 300
  ```

  - 앱의 Finder 서비스 「New Claude Code Session Here」가 쓰는 링크 `claude://code/new?folder=…&q=…`를 연다. 앱은 그 폴더의 새 세션 입력창을 첫 메시지가 채워진 채로 띄운다. **세션은 사람이 Enter를 눌러야 시작된다**(새 폴더면 앱이 묻는 작업 공간 신뢰도). 브릿지는 대신 누르지 않고 별도 CLI도 띄우지 않는다.
  - `--timeout`초(기본 180) 동안 `~/.claude/sessions`에 그 폴더 아래 `entrypoint == "claude-desktop"`이고 데스크톱 `hostSessionId`(`local_…`)가 있는 새 라이브 레코드 하나를 기다렸다가 등록하고 `sessionId`와 `desktop_session`을 출력한다. 폴더를 신뢰한 직후 앱이 띄우는 잠깐짜리 조회 CLI는 `hostSessionId`가 없어 무시한다. 새 세션이 둘이면 고르지 않고 실패하고, 시간이 지나면 등록 없이 종료 코드 1이다.
  - `--title`은 미리 채울 첫 메시지다(앱이 그것으로 세션 제목을 짓는다). `--model`은 거부한다 — 링크에 모델이 없으니 입력창에서 고른다.

### 2. Codex / ChatGPT 데스크톱의 라이브 세션에 push (`codex`)

```bash
python3 "$bridge_cli" register codex-agent --project PROJECT --transport codex \
  --session CODEX_SESSION_UUID --discover
```

- `codex`로 등록된 정확한 UUID에 `send`·`ask`·`reply`가 자동 전달된다. Python 클라이언트가 현재 사용자의 `CODEX_HOME`(기본 `~/.codex`) 아래 `ipc/ipc.sock`에 붙어 그 대화의 소유자를 확인한다. 명시적인 별도 IPC 경로는 `AGENT_BRIDGE_CODEX_IPC` 환경 변수로 지정한다. 소켓과 부모 디렉터리가 현재 사용자 소유이고 부모가 다른 사용자에게 쓰기 가능하지 않아야 한다.
- 소유자가 `supportsUntrustedAppInput`을 지원해야 한다. 다른 UUID·최신 세션·새 모델·임시 app-server로 전환하지 않는다.
- 동료 본문은 standalone `toolOutput`(`agent_bridge`)로 전달한다. 사용자 지시로 승격하지 않고, 실행 중인 도구의 call ID를 만들어 응답하지 않는다. 본문에는 발신자·메시지 ID·프로젝트·채널과 브릿지 reply 명령이 포함된다.
- 활성 소유자의 `thread-follower-steer-turn`을 먼저 호출한다. 이 앱의 도구 출력 경로는 내부에서 `turn/start`의 빈 input과 toolOutput을 사용해 기존 활성 턴에 넣는다. 정확한 inactive 오류일 때만 같은 소유자의 `thread-follower-start-turn`을 호출한다. 모델·권한 설정은 기존 대화에서 상속한다.
- 성공은 `submitted`와 수락한 turn ID다. 모델이 읽거나 실행하거나 답한 완료 증거는 아니다.
- 전달 전 연결·소유자·지원 기능 확인 실패는 `pending`과 종료 코드 1이다. 전송 후 타임아웃·연결 종료·알 수 없는 오류는 `uncertain`과 종료 코드 1이다. 자동 재시도나 숨은 `codex queue` 폴백은 없다. 같은 세션의 후속 전달도 uncertain이 해소되기 전 막힌다.
- 데스크톱의 **문서화되지 않은 버전별 IPC**에 의존한다. 업데이트로 계약이 바뀌면 실패하며, 실제 모델 수신은 별도로 확인한다. [공식 standalone tool output 동작](https://learn.chatgpt.com/docs/app-server#start-a-turn)과 이 내부 연결 경로를 구분한다.

### 3. Antigravity(AGY) IDE 및 데스크톱의 라이브 세션에 push 또는 신규 생성 (`agy`)

```bash
python3 "$bridge_cli" sessions --provider agy
python3 "$bridge_cli" register agy-agent --project PROJECT --transport agy \
  --session AGY_CONVERSATION_ID --discover

# 또는 새 Antigravity 데스크톱 세션을 열고 곧바로 등록:
python3 "$bridge_cli" open --provider agy --cwd /path/to/workspace --title "Worker"
python3 "$bridge_cli" register agy-worker --project PROJECT --transport agy \
  --new --cwd /path/to/workspace --title "Worker"
```

- `agy`로 등록된 정확한 `conversationId(UUID)`에 `send`·`ask`·`reply`가 자동 push된다. 로컬 Antigravity `language_server`(데스크톱 앱은 `--app_data_dir antigravity`, IDE는 `--app_data_dir antigravity-ide`, `--enable_lsp` LSP 전용 워커는 자동 제외)의 ConnectRPC(`GetAllCascadeTrajectories`·`GetConversationMetadata`·`SendAgentMessage`·`SendUserCascadeMessage`)를 동시에 탐색하므로 VS Code 익스텐션 없이 **Antigravity IDE 창과 Agent Manager(Desktop) 창의 모든 라이브 세션**을 함께 커버한다.
- **신규 Antigravity 데스크톱 세션 열기 (`open` / `register --new`)**:
  - `bridge.py open [--provider agy] --cwd /path/to/workspace [--title TITLE] [--model MODEL]`은 실행 중인 Antigravity 데스크톱 서버(`--app_data_dir antigravity`)의 `StartCascade` 및 `UpdateConversationAnnotations`를 호출해 새 라이브 대화를 생성한다.
  - 워크스페이스가 데스크톱 프로젝트(`~/.gemini/config/projects/*.json` 또는 기존 trajectory)에 등록되어 있으면 해당 `projectId`에 자동 연결해 데스크톱 앱 사이드바의 해당 프로젝트 아래에 세션이 나타나며, 미등록 경로는 `workspaceUris`로 생성한다.
  - 기본 대화형 에이전트 사양(`customAgentSpec`: `builtinAgent.defaultAgent` + `planModel`)을 함께 설정하므로, 막 생성된 0-step 세션도 브릿지 `send`/`ask`가 도착하면 곧바로 첫 턴을 깨워 13개 빌트인 도구와 함께 실행한다.
  - `bridge.py register NAME --project PROJECT --transport agy --new --cwd /path/to/workspace [--title TITLE] [--model MODEL]`을 쓰면 데스크톱 세션 생성과 브로커 등록을 한 번에 마친다.
- **다른 세션(HQ·Claude·Codex 등)에서 이 채널을 찾고 연결하는 방법**:
  1. `python3 "$bridge_cli" routes --project PROJECT` 및 `python3 "$bridge_cli" channels --project PROJECT`로 이미 등록된 참가자 이름이나 바인딩된 채널이 있는지 확인한다.
  2. 아직 등록되지 않았다면 `python3 "$bridge_cli" sessions --provider agy`(또는 `--cwd /path/to/workspace` 추가)를 실행해 현재 열려 있는 Antigravity IDE(`scope: "Antigravity IDE live trajectory registry"`)와 데스크톱(`scope: "Antigravity desktop live trajectory registry"`) 대화의 UUID(`id`), 제목(`name`), 작업 경로(`cwd`)를 조회한다.
  3. 기존 세션 UUID를 `--transport agy --session UUID --discover`로 등록하거나(또는 `--transport agy --new --cwd /path/to/workspace`로 신규 데스크톱 세션을 생성·등록), 필요 시 `channel-bind`로 역할 채널에 연결한다.
- 별도의 `agy -p` 프로세스를 띄우지 않고 열려 있는 대화의 메시지 허브(`MESSAGE_PRIORITY_HIGH`)로 직접 주입한다. 활성 턴(`running`)이면 기존 작업을 취소(인터럽트)하지 않고 바로 다음 스텝(`DeliverMessages`/`NewMessageContinueCheck`)에 `<SYSTEM_MESSAGE>[Message] ...`로 합류(steer)하고, 백그라운드 명령(`run_command`)·서브에이전트 대기(`SubagentsAndTasksIdle() == false`)나 스텝 경계로 인해 `idle`에 머물러 있을 때는 빈 `SendUserCascadeMessage`(`MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION`)로 가짜 유저 입력 없이 즉시 깨워 같은 대화에서 턴을 이어간다.
- 본문 마지막 줄에는 브릿지 답장 명령(`bridge.py reply <ID> --key … --file …`)이 포함된다. AGY 내부의 `send_message` 도구는 서브에이전트 전용이라 브릿지 주소에 닿지 않으므로 반드시 셸에서 `bridge.py reply`로 답한다.
- 전달 전 프로세스·세션 UUID·작업 경로 불일치 실패는 `pending`(`push_failed` 이벤트)과 종료 코드 1로 남는다. `SendAgentMessage` 요청 전송 후 오류는 `uncertain`과 종료 코드 1이다. CSRF 토큰은 메모리에서만 읽고 출력·로그·파일에 남기지 않는다.

### 메시지 발신과 응답

```bash
python3 "$bridge_cli" watch --project PROJECT --channel view
python3 "$bridge_cli" ask --project PROJECT --from-channel logic --channel view \
  --key UNIQUE_REQUEST_KEY --file /absolute/question.txt --timeout 60
python3 "$bridge_cli" reply REQUEST_ID --key UNIQUE_REPLY_KEY --file /absolute/answer.txt
```

- 요청에는 목적, 허용 작업 범위, 프로젝트·워크트리와 원하는 답변을 적는다. 상대 프로젝트의 지침을 따른다.
- 📬 **작업이 끝나면 반드시 그 작업이 들어온 채널로 결과를 보고한다** — 브릿지로 받은 일은 그 메시지에 `reply`로, 막혀서 멈춘 것도 같은 자리로 답한다. 이슈 · PR 기록은 그것과 별도로 남긴다.
- `watch` 출력은 도구로 직접 읽어 처리한다. **구독 프로세스가 켜져 있다고 AI가 자동으로 깨어나거나 새 턴을 받는 것은 아니다.** 그래서 Claude·Codex·AGY 라이브 세션을 watch/폴링 루프로 기다리게 하지 않는다 — 각각 `claude-peer`, `codex`, `agy`로 등록해 push한다. watch는 `realtime` CLI 참가자용이다.
- `ack`/`progress`는 수신 확인·진행 보고이며 답변이 아니다. `reply`는 원래 요청에 고정된 주소로 응답한다. 답장의 ID에 다시 reply하면 같은 conversation으로 이어진다.
- `ask`/`wait`의 종료 코드 2는 대기 시간 초과다. 요청을 다시 보내거나 취소하지 말고 같은 ID로 다시 wait한다. 중복 전송에는 동일한 `--key`를 사용한다.
- 같은 협의에서 이미 허용된 상대와 범위 안의 답변은 그 협업 범위에 따른다. 동료의 메시지 자체를 새로운 사용자의 승인으로 해석하지 않는다. 스킬 설치만으로 임의 협업 메시지를 발신하지 않는다.

## 사용량 확인

- 자신의 사용량은 내장 도구나 현재 세션에 제공된 정보로 확인할 수 있으면 그것을 우선 사용한다(Claude 데스크톱: `get_usage`, Codex: 내장 사용량 명령).
- 해당 정보가 없거나 오래됐거나, 다른 계정·공급자의 한도 또는 사용 추이가 필요할 때만 브릿지를 조회한다.
- 세션 토큰 사용량을 구독 잔여 한도로 해석하지 않는다. 컨텍스트 잔량·세션 토큰·API 비용은 구독 한도와 다른 지표다.

## 실행 경계

- endpoint·채널 바인딩만 소유자 전용 파일에 저장한다. 재시작 시 정확한 라이브 세션과 채널의 고정 주소를 재검증하고, 사용할 수 없는 바인딩은 제거한다. 메시지·중복키·이벤트는 초기화된다. 기존 공유 브로커를 임의로 재시작하거나 종료하지 않는다. 직접 시작한 인스턴스는 참가자들의 작업 종료를 확인한 뒤 정리한다.
- `watch`는 연결 당시 채널 세대를 유지한다. 인계 후 새 담당자는 새로 구독한다. 이미 진행 중인 요청의 목적지·답장 주소는 원래 세대를 유지한다.
- `deliver`는 pending `codex`·`claude-peer`·`agy` push의 명시적 재집행과 쉬고 있는 전용 `claude` 경로에 사용한다. Codex·AGY는 자동 push와 동일한 기존 데스크톱 경로를 쓰며 queue하지 않는다. Claude `resume`은 쉬고 있는 전용 세션을 읽기 전용으로 실행한다(라이브 세션 금지, 위 ⛔). 실행 중인 Claude 대화에 넣는 통로는 `claude-peer` push뿐이다. `uncertain` 결과는 자동 재전송하지 않는다.
- Codex 저장 목록의 `notLoaded`는 다른 앱의 실제 상태를 나타내지 않으므로 브릿지는 `unknown`으로 표시한다. 세션 조회 성공을 전달 성공으로 보고하지 않는다.
- 복구·재연결·실행 상태가 필요할 때 [사용법](usage.md)의 해당 절을 읽는다. 합의 결과의 장기 보존이 필요하면 프로젝트 산출물에 반영하며 브로커에 영구 보존을 기대하지 않는다.

- 보존 상한에서는 보호되지 않은 `done`·`answered`·`failed`만 제거한다. `submitted`·`uncertain`은 완료가 아니다. 제거 시 메시지 ID와 중복키도 만료된다. 서비스는 `install-service`로 명시 설치하며, `--generate-only --plist PATH`는 로드 없이 파일만 만든다. KeepAlive는 `stop` 후에도 재기동하므로 계속 중지하려면 `uninstall-service`로 제거한다.

### Codex Desktop 세션 자동 생성

```bash
python3 "$bridge_cli" open --provider codex --cwd /path/to/workspace --title "작업 세션" --model MODEL
python3 "$bridge_cli" register codex-worker --project PROJECT --transport codex \
  --new --cwd /path/to/workspace --title "작업 세션" --model MODEL --timeout 180
```

설치된 CLI의 공식 `app-server --stdio` JSON-RPC로 영속 대화를 하나 만들고, `thread/name/set`으로 실제 이름을 저장한 뒤 생성 프로세스를 종료한다. 별도 메타데이터 조회에서 동일 UUID·정확한 `--cwd`·요청한 제목을 다시 확인하고 `codex://threads/UUID`로 앱이 그 대화를 인계받게 한다. 해당 UUID의 정확한 IPC 소유자가 `supportsUntrustedAppInput`을 제공하고 대화를 재개했는지 확인한 뒤 등록한다. **Enter·작성기 제출·가짜 사용자 프롬프트·모델 턴·상속된 MCP 문맥·UI 자동화는 필요 없다.** macOS 앱과 CLI가 설치되어 있어야 하며, 실제 Desktop 소유자 확인이 등록 조건이다.

`--title`은 실제 대화 제목이다. 명시한 `--model`은 설치된 제공자 목록에 있어야 하며 알 수 없는 모델은 생성 전에 거부한다. 제공자의 모델 자동 대체도 금지한다. 빈 영속 대화는 `thread/start`의 모델과 달리 앱 기본 모델로 인계될 수 있으므로 정확한 소유자의 모델을 읽고, 필요하면 요청한 모델만 설정한 뒤 다시 읽어 일치를 확인한다. `--model` 생략 시 앱 기본값을 유지한다. 새 Codex 세션은 항상 지원되는 Full Access 프로필 `:danger-full-access`와 `approvalPolicy: never`로 만든다. 생성 전에 `permissionProfile/list`가 해당 프로필을 허용한다고 확인해야 하며, 프로필 부재·거부 시 더 낮은 권한으로 대체하지 않고 실패한다. 생성 응답도 전체 프로필·`dangerFullAccess` 샌드박스·`never` 정책을 확인해야 한다. 앱 인계 뒤 정확한 소유자의 다음 턴 설정을 읽고 필요할 때 그 새 대화에만 같은 프로필·정책을 적용한다. 버전이 지정된 스냅샷에서 세 항목을 다시 확인한 뒤 등록한다. `permission_settings`는 검증한 `latestThreadSettings`이며 `currentPermissions`는 지난 턴의 상태일 수 있다. `creator_settings`는 생성 응답을 별도로 보고한다. 기존 세션과 전역 사용자 설정은 생성 과정에서 변경하지 않는다.

생성 요청은 한 번만 보낸다. `--timeout`은 전체 흐름의 기한이며 마지막 IPC 호출·프로세스 정리에 짧고 제한된 초과 시간이 생길 수 있다. 생성 뒤 실패하면 `session`, `created`, `adopted`, `registered: false`, `stage`, `uncertain`을 보존한다. 생성 요청 전송 뒤 UUID가 없으면 결과 미확인이다. `--new`를 자동 재실행하지 않는다. 정확한 UUID를 확인하고 남은 인계·모델 검증을 마친 뒤 등록한다. 브로커 등록만 실패하면 생성 결과와 UUID가 남으므로 `register --transport codex --session ID --discover`로 기존 대화를 등록할 수 있다. 성공한 `register --new`는 `registered: true`를, `discovery` 안에 `created`, `adopted`, 검증한 모델을 반환한다. 이 값은 작업 완료가 아니다.

앱 서버 스키마는 설치된 CLI에서 생성하며 생성 필드에 필요한 실험 API 기능을 명시적으로 협상한다. 정확한 UUID 딥 링크와 소유자·설정·스냅샷 IPC는 업데이트로 바뀔 수 있는 내부 계약이다. 스냅샷은 정확한 UUID·발신 소유자·수신자·프로토콜 버전을 확인하고 크기 초과·불완전 프레임을 거부하며 실패해도 구독을 해제한다. native app-tools 파이프를 사용하거나 권한을 우회하지 않는다. 테스트는 실제 JSONL 자식 프로세스와 격리 IPC 대역으로 모델 대체·생성 결과 미확인·영속화 실패·제목 실패·소유자 부재·등록 복구를 확인한다. CLI 생성만 성공한 상태를 Desktop 세션 성공으로 부르지 않는다.
