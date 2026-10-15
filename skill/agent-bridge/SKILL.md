---
name: agent-bridge
description: Codex(ChatGPT Desktop)·Claude Code·Antigravity(AGY) 데스크톱 및 CLI 세션을 조회하고 프로젝트 채널을 연결해 실시간 질문·답변·진행 상황을 주고받는다. 사용자가 에이전트 간 협업, 채널 연결, 세션 인계 또는 브릿지 운영을 요청할 때 사용한다. 단순 언급이나 일반 코드 작업은 발신 요청이 아니다.
---

# Agent Bridge

같은 머신의 **Claude Code (`claude-peer`) ↔ Codex / ChatGPT Desktop (`codex`) ↔ Antigravity Desktop (`agy`)** 및 CLI 참가자(`realtime`/`claude`)가 함께 쓰는 로컬 실시간 브릿지다. 특정 발신자에 종속되지 않으며, 어느 쪽에서든 상대방의 열려 있는 라이브 세션으로 직접 메시지를 push해 **기존 작업을 중단(인터럽트)하지 않고 3자간(다자간) 실시간 질문·답변·진행 보고**를 주고받는다.

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
   - **Antigravity Desktop (`agy`)**: 실행 중인 데스크톱 대화의 메시지 허브(`SendAgentMessage`)로 즉시 주입해 활성 턴의 다음 스텝에 합류시키거나, 유휴/백그라운드 대기 시 빈 `SendUserCascadeMessage`(`NEXT_INVOCATION`)로 즉시 깨운다.
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

채널 예시는 `view`, `logic`, `review`다. 프로젝트 이름과 실제 맡은 역할에 맞게 정한다. 소켓 구독만 사용하는 CLI 참가자는 기본 `realtime`과 `--cwd`로 등록한다. Claude 라이브 세션은 `--transport claude-peer --session UUID --discover`, Codex 데스크톱 세션은 `--transport codex --session UUID --discover`, Antigravity 데스크톱 세션은 `--transport agy --session UUID --discover`로 등록한다.

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
- ⛔ **`--transport claude`(resume)는 라이브 세션에 쓰지 않는다.** 그것은 살아 있는 대화에 들어가는 길이 아니라 **별개의 Claude CLI 사본을 띄운다**. 쉬고 있는 전용 CLI 세션에만 남겨 둔 경로다. 자세한 등록·인계 명령은 [사용법](references/usage.md)의 **시작**, **세션 조회와 확인**, **사령탑 세션 인계**를 읽는다.

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

### 3. Antigravity(AGY) 데스크톱의 라이브 세션에 push (`agy`)

```bash
python3 "$bridge_cli" register agy-agent --project PROJECT --transport agy \
  --session AGY_CONVERSATION_ID --discover
```

- `agy`로 등록된 정확한 `conversationId(UUID)`에 `send`·`ask`·`reply`가 자동 push된다. 로컬 Antigravity 데스크톱 `language_server`(`--app_data_dir antigravity`)의 ConnectRPC(`GetAllCascadeTrajectories`·`GetConversationMetadata`·`SendAgentMessage`·`SendUserCascadeMessage`)를 사용한다.
- 별도의 `agy -p` 프로세스를 띄우지 않고 데스크톱 창에 열려 있는 대화의 메시지 허브(`MESSAGE_PRIORITY_HIGH`)로 직접 주입한다. 활성 턴(`running`)이면 기존 작업을 취소(인터럽트)하지 않고 바로 다음 스텝(`DeliverMessages`/`NewMessageContinueCheck`)에 `<SYSTEM_MESSAGE>[Message] ...`로 합류(steer)하고, 백그라운드 명령(`run_command`)·서브에이전트 대기(`SubagentsAndTasksIdle() == false`)나 스텝 경계로 인해 `idle`에 머물러 있을 때는 빈 `SendUserCascadeMessage`(`MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION`)로 가짜 유저 입력 없이 즉시 깨워 같은 데스크톱 대화에서 턴을 이어간다.
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

## 실행 경계

- 상태는 메모리에만 있다. 브로커 재시작 시 채널, 메시지, 중복키가 초기화된다. 기존 공유 브로커를 임의로 재시작하거나 종료하지 않는다. 직접 시작한 인스턴스는 참가자들의 작업 종료를 확인한 뒤 정리한다.
- `watch`는 연결 당시 채널 세대를 유지한다. 인계 후 새 담당자는 새로 구독한다. 이미 진행 중인 요청의 목적지·답장 주소는 원래 세대를 유지한다.
- `deliver`는 pending `codex`·`claude-peer`·`agy` push의 명시적 재집행과 쉬고 있는 전용 `claude` 경로에 사용한다. Codex·AGY는 자동 push와 동일한 기존 데스크톱 경로를 쓰며 queue하지 않는다. Claude `resume`은 쉬고 있는 전용 세션을 읽기 전용으로 실행한다(라이브 세션 금지, 위 ⛔). 실행 중인 Claude 대화에 넣는 통로는 `claude-peer` push뿐이다. `uncertain` 결과는 자동 재전송하지 않는다.
- Codex 저장 목록의 `notLoaded`는 다른 앱의 실제 상태를 나타내지 않으므로 브릿지는 `unknown`으로 표시한다. 세션 조회 성공을 전달 성공으로 보고하지 않는다.
- 복구·재연결·실행 상태가 필요할 때 [사용법](references/usage.md)의 해당 절을 읽는다. 합의 결과의 장기 보존이 필요하면 프로젝트 산출물에 반영하며 브로커에 영구 보존을 기대하지 않는다.

