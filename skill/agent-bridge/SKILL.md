---
name: agent-bridge
description: Codex와 Claude Code 사령탑의 세션을 조회하고 프로젝트 채널을 연결해 실시간 질문·답변·진행 상황을 주고받는다. 사용자가 에이전트 간 협업, 채널 연결, 세션 인계 또는 브릿지 운영을 요청할 때 사용한다. 단순 가족 언급이나 일반 코드 작업은 발신 요청이 아니다.
---

# Agent Bridge

같은 머신의 Codex와 Claude Code가 함께 쓰는 로컬 실시간 브릿지다. 특정 발신자에 종속되지 않는다. 현재 에이전트의 이름과 실제 담당 세션을 사용한다.

## 실행 경로

이 스킬의 `scripts/bridge.py`가 진입점이다. Python 3.10 이상, macOS/Linux에서 외부 패키지 없이 실행한다. 설치된 공용 경로는 다음과 같다.

```bash
bridge_cli="$HOME/.agents/skills/agent-bridge/scripts/bridge.py"
python3 "$bridge_cli" --help
```

다른 위치에 설치되어 공용 경로가 없다면 **읽은 SKILL.md의 형제 scripts/bridge.py**를 사용한다. 세 스크립트는 같은 폴더에 유지한다. 작업 프로젝트로 이동하거나 스크립트를 복사할 필요는 없다.

## 협업 연결

1. `sessions`로 실제 세션을 찾고 `session`으로 정확한 UUID·제목·작업 경로를 확인한다. 여러 후보가 있으면 현재 작업과 사람의 지시로 구별한다. 제목의 유사성이나 최신순만으로 상대를 선택하지 않는다.
2. `ping`으로 브로커를 확인한다. 이미 실행 중이면 재사용한다. 연결이 없고 브릿지 운영이 요청 범위라면 `serve`를 실행하고 `ready` 결과를 확인한다. `serve`는 장기 실행 명령이므로 실행 도구의 세션을 유지하며 관리한다.
3. `register`로 양쪽 이름과 연결을 등록하고, `channel-bind`로 프로젝트별 작업 채널을 연결한다. 등록 전에 `routes`/`channels`를 확인해 다른 사령탑의 연결을 덮어쓰지 않는다.
4. 수신 측에서 `watch`를 연결하고 `subscribed`를 확인한다. 발신은 `send` 또는 `ask`, 답변은 `reply REQUEST_ID`로 연결한다.

```bash
python3 "$bridge_cli" sessions --cwd /absolute/project
python3 "$bridge_cli" session codex SESSION_UUID
python3 "$bridge_cli" ping
# 브로커가 없을 때 실행하고, 반환되는 실행 도구 세션을 관리한다.
python3 "$bridge_cli" serve
```

채널 예시는 `view`, `logic`이다. 프로젝트 이름과 실제 맡은 역할에 맞게 정한다. CLI 세션을 확인해 등록할 때는 `--transport codex` 또는 `--transport claude`와 `--session UUID --discover`를 사용한다. 소켓 구독만 사용하는 참가자는 기본 `realtime`과 `--cwd`로 등록한다. 자세한 등록·인계 명령은 [사용법](references/usage.md)의 **시작**, **세션 조회와 확인**, **사령탑 세션 인계**를 읽는다.

## 질문과 답변

```bash
python3 "$bridge_cli" watch --project PROJECT --channel view
python3 "$bridge_cli" ask --project PROJECT --from-channel logic --channel view \
  --key UNIQUE_REQUEST_KEY --file /absolute/question.txt --timeout 60
python3 "$bridge_cli" reply REQUEST_ID --key UNIQUE_REPLY_KEY --file /absolute/answer.txt
```

- 요청에는 목적, 허용 작업 범위, 프로젝트·워크트리와 원하는 답변을 적는다. 상대 프로젝트의 지침을 따른다.
- `watch` 출력은 도구로 직접 읽어 처리한다. **구독 프로세스가 켜져 있다고 AI가 자동으로 깨어나거나 새 턴을 받는 것은 아니다.** 기다리는 동안 들어온 요청도 별도 watch 연결로 받을 수 있다.
- `ack`/`progress`는 수신 확인·진행 보고이며 답변이 아니다. `reply`는 원래 요청에 고정된 주소로 응답한다. 답장의 ID에 다시 reply하면 같은 conversation으로 이어진다.
- `ask`/`wait`의 종료 코드 2는 대기 시간 초과다. 요청을 다시 보내거나 취소하지 말고 같은 ID로 다시 wait한다. 중복 전송에는 동일한 `--key`를 사용한다.
- 같은 협의에서 이미 허용된 상대와 범위 안의 답변은 그 협업 범위에 따른다. 동료의 메시지 자체를 새로운 사람의 승인으로 해석하지 않는다. 스킬 설치만으로 임의 협업 메시지를 발신하지 않는다.

## 실행 경계

- 상태는 메모리에만 있다. 브로커 재시작 시 채널, 메시지, 중복키가 초기화된다. 기존 공유 브로커를 임의로 재시작하거나 종료하지 않는다. 직접 시작한 인스턴스는 참가자들의 작업 종료를 확인한 뒤 정리한다.
- `watch`는 연결 당시 채널 세대를 유지한다. 인계 후 새 담당자는 새로 구독한다. 이미 진행 중인 요청의 목적지·답장 주소는 원래 세대를 유지한다.
- `deliver`는 별도 동작이다. Codex `queue` 성공은 CLI 접수이고, Claude `resume`은 쉬고 있는 전용 세션을 읽기 전용으로 실행한다. 실행 중인 상대 대화에 알림을 주입하는 통로로 취급하지 않는다. `uncertain` 결과는 자동 재전송하지 않는다.
- Codex 저장 목록의 `notLoaded`는 다른 앱의 실제 상태를 나타내지 않으므로 브릿지는 `unknown`으로 표시한다. 세션 조회 성공을 전달 성공으로 보고하지 않는다.
- 복구·재연결·실행 상태가 필요할 때 [사용법](references/usage.md)의 해당 절을 읽는다. 합의 결과의 장기 보존이 필요하면 프로젝트 산출물에 반영하며 브로커에 영구 보존을 기대하지 않는다.

## 기존 가족 스킬과 구분

이 스킬은 로컬 사령탑의 채널 기반 실시간 협업을 담당한다. 지니·루나(AGY)의 개별 호출·기억 요청은 `gemini-family-bridge`가 맡는다(옛 `ai-family-bridge`는 2026-10-13 폐기 — 그 스킬의 Claude CLI 경로는 사령탑이 아니라 별개 부대를 띄웠다). Claude 사령탑과의 소통은 이 스킬로만 하고, Claude CLI를 직접 띄우지 않는다. 이 스킬은 다른 스킬의 스크립트나 설치에 의존하지 않는다.

(나루, 2026-10-01 — Codex·Claude Code 공용 전역 스킬)
