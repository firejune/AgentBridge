<p align="center">
  <img src="../../assets/banner.svg" alt="Agent Bridge — Claude, Codex, Antigravity를 위한 실시간 로컬 브릿지" width="100%" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-38BDF8.svg?style=flat-square" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/dependencies-zero%20(stdlib)-30D158.svg?style=flat-square" alt="Zero Dependencies" />
  <img src="https://img.shields.io/badge/peers-claude--peer%20%C2%B7%20codex%20%C2%B7%20agy-FF6B4A.svg?style=flat-square" alt="Supported Peers" />
  <img src="https://img.shields.io/badge/MCP-Phase%201%20%2B%20Inbox-A855F7.svg?style=flat-square" alt="MCP Phase 1" />
</p>

<p align="center">
  <a href="../../README.md">English</a> · <strong>한국어</strong>
</p>

같은 머신의 **Claude Code / Desktop (`claude-peer`) ↔ Codex / ChatGPT Desktop (`codex`) ↔ Antigravity IDE / Desktop (`agy`)** 및 CLI 참가자(`realtime`/`claude`)가 실시간으로 질문·답변·진행 상황을 주고받는 로컬 브릿지다. Python 3.10+ 표준 라이브러리와 Unix 도메인 소켓만 사용하며 외부 패키지, API 키, 네트워크 포트가 필요 없다.

별도의 CLI 사본을 중복 실행하거나 라이브 세션을 `watch` 폴링 루프에 가두지 않고, 열려 있는 상대 세션으로 메시지를 직접 push해 진행 중인 작업을 중단하지 않으면서 다자간 협업을 수행한다.

## 문서 구성

| 대상 | English | 한국어 |
| --- | --- | --- |
| 개요 및 빠른 시작 | [README.md](../../README.md) | [docs/ko/README.md](README.md) |
| 에이전트 스킬 지침 | [skill/agent-bridge/SKILL.md](../../skill/agent-bridge/SKILL.md) | [docs/ko/SKILL.md](SKILL.md) |
| 상세 CLI 및 프로토콜 레퍼런스 | [skill/agent-bridge/references/usage.md](../../skill/agent-bridge/references/usage.md) | [docs/ko/usage.md](usage.md) |
| MCP Phase 1 명세 | [skill/agent-bridge/references/mcp-phase1.md](../../skill/agent-bridge/references/mcp-phase1.md) | — |
| Antigravity IDE 내부 명령어 | — | [docs/ko/antigravity-commands.md](antigravity-commands.md) |

## 아키텍처 및 전송 방식

```mermaid
flowchart LR
    subgraph Live["라이브 세션 참가자 (Direct Push · No Polling)"]
        C["Claude Code / Desktop<br/>(claude-peer)"]
        X["Codex / ChatGPT Desktop<br/>(codex)"]
        A["Antigravity IDE & Desktop<br/>(agy)"]
    end
    B(("로컬 브로커<br/>Unix socket"))
    W["CLI 참가자<br/>(realtime · watch)"]

    C <-->|"send / ask / reply<br/>peerProtocol 1"| B
    X <-->|"send / ask / reply<br/>IPC steer-turn"| B
    A <-->|"send / ask / reply<br/>SendAgentMessage + NEXT_INVOCATION"| B
    W <-->|"send / reply / watch"| B
```

| 환경 | Transport | 전달 메커니즘 |
| --- | --- | --- |
| **Claude Code / Desktop** | `claude-peer` | 살아 있는 세션의 메시징 소켓(`peerProtocol 1`)으로 직접 주입해 별도 CLI 프로세스를 띄우지 않고 다음 턴에 전달한다. |
| **Codex / ChatGPT Desktop** | `codex` | `CODEX_HOME/ipc/ipc.sock`의 정확한 대화 소유자에 연결해 활성 턴이면 `thread-follower-steer-turn`(`toolOutput`)으로 합류하고, 유휴 상태면 새 턴을 시작한다. |
| **Antigravity IDE & Desktop** | `agy` | 로컬 `language_server` / `agy --hub`의 ConnectRPC에 연결해 활성 턴에는 `<SYSTEM_MESSAGE>`(`SendAgentMessage`)로 합류하고, 유휴/대기 상태에는 빈 `SendUserCascadeMessage`(`NEXT_INVOCATION`)로 즉시 깨운다. |
| **CLI 워커** | `realtime` / `claude` | `realtime`은 `watch`로 JSONL 이벤트를 구독한다. `claude`는 **쉬고 있는 전용 CLI 세션**만 `deliver` 시 읽기 전용 `plan` 모드로 `--resume`한다(라이브 세션에는 절대 사용하지 않는다). |

## 글로벌 스킬 설치

[`skills`](https://skills.sh) CLI로 Claude Code, Codex, Antigravity 글로벌 스킬 경로에 한 번에 설치할 수 있다:

```bash
npx skills add firejune/AgentBridge -g -a claude-code codex antigravity
```

또는 저장소를 클론한 뒤 글로벌 스킬 경로에 직접 심볼릭 링크를 연결한다:

```bash
mkdir -p ~/.agents/skills ~/.claude/skills ~/.gemini/config/skills
ln -sfn "$(pwd)/skill/agent-bridge" ~/.agents/skills/agent-bridge
ln -sfn "$(pwd)/skill/agent-bridge" ~/.claude/skills/agent-bridge
ln -sfn "$(pwd)/skill/agent-bridge" ~/.gemini/config/skills/agent-bridge
```

## 빠른 시작

```bash
bridge_cli="$HOME/.agents/skills/agent-bridge/scripts/bridge.py"

# 1. 브로커 상태 확인 및 기동 (기본 소켓: ~/.local/share/agent-bridge/bridge.sock)
python3 "$bridge_cli" ping || python3 "$bridge_cli" serve

# 2. 열려 있는 Claude · Codex · Antigravity 세션 조회
python3 "$bridge_cli" sessions --cwd /path/to/workspace

# 3. 기존 라이브 세션을 정확한 UUID로 등록
python3 "$bridge_cli" register claude-main --project my-app --transport claude-peer \
  --session CLAUDE_UUID --discover
python3 "$bridge_cli" register codex-main --project my-app --transport codex \
  --session CODEX_UUID --discover
python3 "$bridge_cli" register agy-main --project my-app --transport agy \
  --session AGY_UUID --discover

# 4. 또는 새 라이브 세션을 열면서 즉시 등록
python3 "$bridge_cli" register agy-worker --project my-app --transport agy \
  --new --cwd /path/to/workspace --title "Worker"

# 5. 역할 채널 바인딩 및 질문·답변 교환
python3 "$bridge_cli" channel-bind logic --project my-app --owner claude-main
python3 "$bridge_cli" channel-bind view --project my-app --owner agy-main

python3 "$bridge_cli" ask --project my-app --from-channel logic --channel view \
  --key req-001 --file /path/to/request.txt --timeout 60
python3 "$bridge_cli" reply MESSAGE_ID --key reply-001 --file /path/to/answer.txt
```

## 핵심 기능

### 1. 세션 생명주기 (`sessions`, `open`, `register --new`, `close`)

| 공급자 | 데스크톱 세션 열기 | IDE 세션 열기 (`--host ide`) | 세션 닫기 (`bridge.py close NAME --project P [--archive]`) |
| --- | --- | --- | --- |
| **Antigravity (`agy`)** | `language_server` / `agy --hub`(`StartCascade`)로 무인 생성 및 프로젝트·워크트리 환경 자동 연결 | 데스크톱과 동일 (`StartCascade` + 통합 허브 프로젝트·환경 자동 연결) | 기본은 대화 스택 유지(`--archive` 시 `{archived: true}` 보관) 후 라우트 제거 |
| **Claude (`claude-peer`)** | `claude://code/new`로 첫 메시지를 채운 입력창을 열고 사용자의 Enter 대기 | IDE CLI + `<scheme>://anthropic.claude-code/open`으로 무인 생성 | CLI 프로세스에 `SIGTERM` 전송 후 기본은 대화 스택 유지(`--archive` 시 데스크톱 `isArchived: true` 보관) 및 라우트 제거 |
| **Codex (`codex`)** | `app-server --stdio` 생성 후 `codex://threads/UUID`로 데스크톱 자동 인계 (`:danger-full-access`, `approvalPolicy: never`) | 미지원 | 기본은 사이드바 대화 유지(`--archive` 시 `state_5.sqlite` `archived=1` 및 IPC 알림) 후 라우트 제거 |

```bash
# 단독 생성 또는 생성과 동시에 브로커 등록:
python3 "$bridge_cli" open --provider agy --host ide --cwd /path/to/workspace --title "Worker"
python3 "$bridge_cli" register codex-worker --project my-app --transport codex \
  --new --cwd /path/to/workspace --title "Worker" --model gpt-6.1-sol

# 작업이 끝난 워커 세션 닫기 및 라우트 해제 (사이드바에서 숨기려면 --archive 추가):
python3 "$bridge_cli" close agy-worker --project my-app [--archive]
```

`bridge.py remap`은 브로커의 모든 프로젝트에서 보관되지 않은 등록 Codex 세션을 다시 연다. 저장된 작업 디렉터리를 확인하고 Desktop 소유자가 이미 붙은 세션은 건너뛴다. 결과는 엔드포인트별로 출력한다. 세션을 다시 열 때 Codex Desktop이 화면 앞으로 올 수 있다. UUID와 대기 중인 메시지는 그대로 유지한다.

`bridge.py remap NAME --project PROJECT --expect-session OLD_UUID --session NEW_UUID`로 등록된 Codex 엔드포인트의 UUID를 교체할 수 있다. 새 세션에는 라이브 데스크톱 소유자가 있어야 하며 작업 디렉터리는 기존과 같아야 한다. 참가자 이름으로 직접 보내는 새 메시지에 교체된 UUID를 사용한다. 채널 바인딩과 기존 메시지는 원래 라우트를 유지한다.

Codex 공급자 실패는 등록된 모든 Codex 스레드를 대상으로 관찰하며 관찰 회차 사이 기본 간격은 10초다. `serve --codex-failure-interval SECONDS`로 주기를 정하며 `0`은 관찰을 끈다. 등록된 스레드·현재 Desktop 소유자·최신 최종 턴를 고정하고, 실행 상태가 idle이며 최신 턴의 완료 상태가 `failed`이고 일치하는 최종 오류가 `willRetry: false`일 때 알려진 네트워크·스트림 실패를 감지한다. 원래 메시지의 고정 반환 주소로 정규화한 `provider_failure_detected` 알림 하나를 남긴다. 소유자의 `inbox`에서 ID를 찾고 `show ID`로 읽거나 MCP inbox를 사용한다. 원본 작업 상태는 유지하며 완료된 도구 호출이 있는 턴도 감지한다.

알림에는 `observed_at`·턴 ID·실패 종류를 담는다. `failed_at`은 턴 시작 시각과 실행 시간이 모두 있으면 합산하고 `failure_time_source: turn_duration`을 표시하며, 자료가 없으면 null이다. 공급자 원문·도구 출력·Desktop 소유자 메타데이터는 알림과 체크포인트에서 제외한다. 관찰은 읽기 전용이며 앱의 capacity 재시도를 따른다. 브로커 재시작 뒤 등록 라우트에서 관찰을 재개한다. 진행 중 작업이 없어도 해당 라우트의 마지막 보관 발신자에게 알린다. 보관 한도 내에 발신자가 없으면 등록 스레드 자신의 inbox에 알림을 남긴다. 보관 한도 내 알림의 중복 방지는 재시작 후에도 유지한다. 소유자 부재·미지원 스냅샷·무응답 시간만으로 공급자 실패를 판정하지 않는다.

### 2. 모델 조회·전환 및 정책 (`model`, `policy`)

공급자 중립 인터페이스로 세션의 모델을 실시간 조회·전환하며, `<state-dir>/policy.json` 정책(기본 시드 거부 목록은 Codex의 `gpt-6-astra` 차단)을 강제한다.

```bash
python3 "$bridge_cli" model NAME --project my-app              # 현재 모델 조회
python3 "$bridge_cli" model NAME --project my-app --set MODEL  # 모델 전환 후 재조회 검증
python3 "$bridge_cli" policy                                   # 적용 중인 기본값 및 거부 목록 확인
```

- **Antigravity (`agy`)**: 마지막 generator의 `plannerConfig`에서 모델을 읽는다. 대기 중 전환 시 기존 플래너 설정(`knowledgeConfig` 등)을 유지한 채 `requestedModel`만 바꿔 1회 호출로 적용한다.
- **Codex (`codex`)**: 정확한 데스크톱 소유자의 다음 턴 모델 설정을 읽고 `thread-follower-update-thread-settings`로 전환 후 재조회로 검증한다.
- **Claude (`claude-peer`)**: 대화 기록의 최신 assistant 메시지에서 모델을 읽는다. 데스크톱 세션 전환은 `policy.json`에 지정된 `command_post` 세션을 통해 `set_session_model`을 중계하며, IDE 세션 전환은 `unsupported`(IDE 모델 선택기 사용)다.

### 3. 구독 한도 및 위험도 모니터링 (`usage`)

브로커는 백그라운드에서 구독 한도를 수집해 `<state-dir>/usage/<provider>.jsonl`에 기록한다. `codex`나 `agy` 경로(또는 `claude-peer`끼리의 경로)를 거치는 모든 `send`·`ask`·`reply` 결과에는 `usage` 상태(`ok`, `at_risk`, `exhausted`, `unknown`)가 함께 반환되어 한도 고갈 전에 작업 속도를 조절할 수 있다.

```bash
python3 "$bridge_cli" usage                        # 전체 공급자(Codex + Antigravity + Claude) 버킷 한도 및 소모율 출력
python3 "$bridge_cli" usage --provider claude     # Claude 데스크톱 계정과 Claude Code(IDE/CLI) 계정
python3 "$bridge_cli" usage --provider agy --json  # Antigravity 버킷(agy/gemini, agy/partner) JSON 조회
python3 "$bridge_cli" usage --events               # 리셋 이력, spend horizon 및 stop-risk horizon 확인
```

**Claude (`claude/desktop`, `claude/ide`)** — Claude 데스크톱과 Claude Code IDE 확장이 서로 다른 계정으로 도는 머신용이다. 어느 읽기도 모델 턴을 시작하지 않는다:

- `claude/desktop`: 데스크톱이 스스로 15분마다 남기는 기록(`~/Library/Application Support/Claude/plan-usage-history.json`), 데스크톱에 로그인된 계정 기준. 사용률만 있고 리셋 시각은 없다.
- `claude/ide`: Claude Code 로그인(IDE 확장 · 터미널 CLI). CLI의 `/usage`가 부르는 사용량 엔드포인트를 실시간으로 읽고, 토큰은 매번 키체인 항목 `Claude Code-credentials`에서 읽는다. 토큰은 저장 · 기록 · 갱신하지 않는다. 토큰이 없거나 만료됐거나 호출이 실패하면 CLI 자체 캐시(`~/.claude.json`)를 쓴다 — 세션 시작이나 `/usage` 때만 쓰여서 낡은 경우가 많으니 `observed`를 본다. `serve --no-claude-live`로 키체인 읽기를 끈다.
- 표본은 원천이 잰 시각을 그대로 가진다 — `observed … ago`는 브로커가 아니라 원천의 나이다.

### 4. 에이전트 워크트리 (`wt`)

메인 저장소 체크아웃은 항상 `main`으로 유지하고, 각 에이전트의 코드 작업은 `<workspace>/.worktrees/<repo>/<agent>-<issue>` 아래 격리된 워크트리에서 수행한다.

```bash
python3 "$bridge_cli" wt new . jinni 42 --slug fix-bug --provider agy --session UUID
python3 "$bridge_cli" wt list --text
python3 "$bridge_cli" wt sweep          # 머지 완료 및 소유 세션이 종료된 워크트리 정리 대상 확인 (dry-run)
python3 "$bridge_cli" wt sweep --apply  # 대상 워크트리 및 브랜치 실제 제거
```

### 5. 라우팅 영속화 및 선택적 `launchd` 서비스

- **소유자 전용 메시지·라우팅 저장**. 재시작은 브로커의 종료 메시지 제거·보호 규칙에 따라 라우트와 메시지를 복원하며, 낮춘 한도를 넘는 보호 메시지는 유지하고 stderr와 `ping.correspondence_over_limit`에 표시한다. 필드·한도·손실 진단은 [저장 계약](../../skill/agent-bridge/references/usage.md#correspondence-storage-issue-94)을 따른다.
- **선택적 macOS `launchd` 관리**: 자동 기동 서비스는 기본 꺼짐 상태다. `install-service`(검토용 파일 생성은 `--generate-only --plist PATH`)와 `uninstall-service`로 `KeepAlive` 에이전트를 관리한다.

### 6. 원격 MCP 파사드 (`mcp_server.py`)

로컬 에이전트(Claude Code, Codex, Antigravity)는 로컬 MCP를 등록하지 않고 `bridge.py` 파이썬 CLI를 직접 사용한다. `skill/agent-bridge/scripts/mcp_server.py`는 비공개 단일 소유자 원격 터널(`stdio` 기반 `--phase1-config /absolute/private.json`) 전용 인터페이스로, 세션 조회·생성, 메시지 전송, 독립 수신함(`inbox_enabled: true`)을 단일 커넥터로 노출한다(`--phase1-config` 생략 시 읽기 전용 `bridge_status`만 응답). 자세한 계약은 [mcp-phase1.md](../../skill/agent-bridge/references/mcp-phase1.md)를 참고한다.

## 개발 및 검증

변경 사항을 커밋하기 전에 전체 단위·통합 테스트 스위트를 실행해 검증한다.

```bash
python3 -m unittest discover -s tests -v
```

## 라이선스

[MIT](../../LICENSE)
