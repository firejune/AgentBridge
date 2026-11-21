# Codex Desktop에서 큐 알림 뒤 작업이 멈출 때

`submitted`와 `delivery_path: "codex-desktop-queue"`가 보이면 Desktop 큐가 알림을 접수한 상태다. 등록된 정확한 수신 대화에서 턴 시작, 본문 조회 성공, 요청한 결과 출력을 각각 확인한다. 대화가 로드되지 않았거나 중단된 상태라면 알림이 큐에 남을 수 있다. 전달이 모호하면 재전송하지 않고 기존 메시지 상태와 수신 대화의 기록을 확인한다. [큐 전달 문제 #156](https://github.com/firejune/AgentBridge/issues/156)

## 생성 시 지정한 권한과 실제 큐 턴이 다를 때

`thread/start` 응답은 생성 프로세스에 적용된 권한을 보여 준다. Desktop은 큐로 스레드를 깨울 때 rollout에 저장된 설정을 읽는다. 저장된 권한이 빠진 경우 Codex Desktop `26.1117.31558`(빌드 `29513`)의 한 검증에서 생성 응답은 `:workspace / on-request`였지만 실제 큐 턴은 `:read-only / on-request`로 실행됐다.

큐로 깨울 새 스레드에 `workspace-write`를 적용하려면 로컬 운영 정책에 `"codex_desktop_wake_permissions": "workspace-write"`를 추가한다. 브리지는 rollout이 생긴 뒤 `thread/settings/update`로 설정을 저장한다. 결과의 `permission_settings`에서 `:workspace`, `on-request`, `networkAccess: false`가 확인된 턴을 기준으로 판단한다. 같은 Desktop 빌드의 격리 검증에서 이 권한으로 실행된 큐 턴은 별도 권한 상승이나 승인 대기 없이 `receive-codex`를 실행했고, 조회가 종료 코드 0으로 성공한 뒤 지정한 확인 문구를 반환했다. [Codex Desktop 세션 설정](usage.md#3-codex-데스크톱-세션-무인-생성-및-등록-codex)

## 본문 조회에 승인이 뜨는 경우

2026년 11월 21일 Codex Desktop `26.1117.31558 (29513)`, 설치된 Agent Bridge CLI `01ed6d7`, 브로커 `1f82299` 조합에서 확인했다. 읽기 전용이며 필요시 승인을 요청하는 큐 턴에서 구버전 `receive-codex`가 로컬 브로커 연결을 거부당했다. 수동 승인 후 본문을 읽었지만, 조회 결과는 `authorization: none`이 붙은 원문이었고 요청된 작업은 실행되지 않았다.

로컬 운영자가 수신 조회 명령의 샌드박스 밖 실행을 미리 승인하는 환경에서는 `~/.codex/rules/default.rules`에 아래와 같이 조회 전용 규칙을 둘 수 있다. 예시의 `/absolute/path/to/bridge.py`는 실제 설치된 `bridge.py`의 절대 경로로 바꾸고, 실행할 때도 같은 경로 문자열을 쓴다. `codex execpolicy check`로 실제 인자 배열을 검사한다. 이 규칙은 해당 스크립트의 `receive-codex` 인자 접두사에만 일치한다. [OpenAI 명령 실행 규칙](https://developers.openai.com/codex/rules)

```python
prefix_rule(
    pattern = ["python3", "/absolute/path/to/bridge.py", "receive-codex"],
    decision = "allow",
)
```

실측에서는 `execpolicy`로 위 접두사를 확인했고 같은 스크립트의 `send`에는 일치하지 않았다. 당시에는 Desktop을 재시작한 뒤 절대 경로로 실행한 샌드박스 밖 조회가 승인 창 없이 성공했다. 이 규칙이 허용하는 범위는 조회 명령 실행까지다. 읽어 온 메시지의 작업 권한은 기존 사용자 승인 범위를 따른다.

## 본문을 읽고도 작업이 멈추는 경우

새 Desktop 대화에서 작업을 끝까지 수행하라는 전역 `AGENTS.md` 지침을 로드했지만, 구버전 큐 턴은 본문 조회에서 끝났다. 같은 모델과 읽기 전용 설정으로 같은 무해한 작업을 로컬 Codex CLI에 직접 입력했을 때는 요청한 결과가 나왔다.

PR #163은 조회 결과에 전체 동료 메시지 봉투를 복구하고, 큐 알림에 기존 사용자 승인 범위에서 조회 후 작업을 이어가도록 안내한다. 발신자 이름은 현재 발신자가 제시하며, 작업 권한은 기존 사용자 승인 범위를 따른다. 이 절의 초기 실측은 수정본 배포 전에 진행했다. 배포 후 전체 작업 수행은 아래 절차로 확인한다. [수정 PR #163](https://github.com/firejune/AgentBridge/pull/163)

## 수정본 배포 후 확인

지원되는 배포 절차로 업데이트한 뒤, 사용자가 승인한 읽기 전용 작업 하나를 정확히 등록된 수신 대화에 한 번 보낸다. 예를 들어 지정한 공개 파일을 읽고 지정한 표식과 확인한 값을 최종 응답에 출력하도록 요청한다. 큐 접수 뒤 같은 대화에서 턴 시작과 본문 조회를 확인하고, 승인 창 없이 최종 응답에 표식과 값이 나온 경우에만 통과로 기록한다.
