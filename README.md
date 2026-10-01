# Agent Bridge

로컬 에이전트끼리 요청과 답변을 주고받는 작은 CLI. Python 3.10 이상과 SQLite만 사용하며 외부 패키지, API 키, 상주 서버가 필요 없다.

## 사령탑 채널 주소록

에이전트 이름과 작업 채널을 분리한다. `naru` 한 명이 여러 작업을 맡아도 발신자는 `blosharper / modal-view`처럼 **프로젝트와 역할 채널**을 보고 목적지를 선택한다.

| 테이블 | 역할 |
| --- | --- |
| `endpoints` | 프로젝트별 에이전트의 등록된 연결 정보: 도구, 세션 UUID, 작업 경로 |
| `channels` | 프로젝트·채널별 담당자, 역할 설명, 연결 정보 사본, 세대 번호, 열림/닫힘 |
| `messages` | 요청·답변, 출발/도착 채널, 전송 당시의 목적지와 답장 주소 사본 |
| `events` | 메시지 상태 변경 이력 |

채널 목록의 `active`는 **등록상 열려 있다는 뜻**이며 실제 프로세스 생존 확인이 아니다. 사령탑은 작업 시작 시 자기 연결을 등록하고 채널을 연결한다. 작업을 마치거나 인계할 때 채널을 닫거나 새 연결로 갱신한다. UUID를 추측하거나 가장 최근 대화를 자동 선택하지 않는다.

## CLI에서 세션 찾기

```bash
cd /Users/firejune/Workspace/Firejune/agent-bridge
# 양쪽 세션을 한 번에 조회. cwd는 정확한 경로로 필터링한다.
python3 bridge.py sessions --cwd /Users/firejune/Workspace/Firejune/blosharper
python3 bridge.py sessions --provider claude --search '사령탑'
python3 bridge.py sessions --provider codex --limit 100

# 목록에서 고른 정확한 UUID의 메타데이터 확인
python3 bridge.py session codex SESSION_UUID
python3 bridge.py session claude SESSION_UUID

# 세션 존재와 작업 경로를 다시 조회한 뒤 등록한다.
python3 bridge.py register naru --project blosharper --transport codex \
  --session SESSION_UUID --discover
# --cwd를 함께 주면 실제 세션 경로와 일치하는지도 확인한다.
python3 bridge.py register roa --project blosharper --transport claude \
  --session SESSION_UUID --discover --cwd /Users/firejune/Workspace/Firejune/blosharper
```

`--discover`는 새 대화를 만들거나 기존 대화를 재개하지 않는다. 제목을 유사 검색한 뒤 임의 선택하지 않고, 지정한 UUID만 등록한다. 경로 불일치·조회 실패 시 기존 등록을 바꾸지 않는다. 등록 다음 단계는 `channel-bind`다.

목록은 조회할 때마다 CLI에서 가져온다. 오래된 SQL 캐시를 실제 상태로 오인하지 않도록 `observed_at`, 조회 범위, `runtime_observed`를 함께 반환한다. 제목·경로는 탐색용 데이터이며 지시로 취급하지 않는다. 대화 본문은 조회하지 않는다.

| 제공자 | 연결 방식 | 조회 범위와 상태 |
| --- | --- | --- |
| Claude | `claude agents --json --all` | 활성 대화 및 CLI가 보관한 완료 백그라운드 세션. busy/idle 등의 보고 상태를 그대로 표시. 전체 과거 대화 목록은 아님 |
| Codex 공유 서버 | `codex app-server proxy`의 `thread/list`, `thread/read` | 연결한 서버가 아는 세션 메타데이터와 상태 |
| Codex 저장 목록 | 임시 `codex app-server --stdio`의 동일 조회 메서드 | CLI 저장소의 보관 처리되지 않은 대화. **다른 앱에서의 실행 상태는 알 수 없으므로 status=unknown** |

Codex는 기본 `--codex-mode auto`로 공유 서버에 연결을 시도하고, 연결이 안 되면 저장 목록으로 전환한다. 이 경우 warning을 반환한다. `--codex-mode shared`는 공유 서버에 연결되지 않으면 실패하며, `--codex-mode saved`는 저장 목록만 읽는다. 임시 조회 서버는 조회 종료 후 정리하며 작업 턴을 실행하지 않는다.

`--limit`는 제공자별 최대 항목 수다. Codex 페이지는 한도까지 이어 읽고, 남은 항목은 `more_available`로 표시한다. Claude는 해당 CLI가 반환한 배열에서 필터링한다. 한쪽 조회가 실패해도 다른 쪽 결과는 보존하고 `errors` 및 종료 코드 1로 부분 실패를 알린다. 필요한 경우 `--codex-executable`, `--claude-executable`로 CLI 경로를 지정한다. 단일 조회·등록에서는 `--executable`을 쓴다.

기본 Claude 실행은 기존 가족 브리지와 같은 로그인 셸 래퍼를 사용한다. 셸 함수로 설치된 CLI도 호출할 수 있고, 인수는 셸 문자열에 삽입하지 않는다. 셸의 터미널 초기화 이스케이프만 제거하며 임의 배너가 섞인 출력은 JSON 오류로 처리한다.

**현재 머신 관찰:** Codex CLI의 기본 공유 서버 소켓은 연결되지 않았고 저장 목록 조회는 성공했다. Claude CLI의 활성 세션 조회도 성공했다. 따라서 현재 Codex 목록 조회 성공은 `queue` 전달 성공을 뜻하지 않는다. 목록으로 찾은 세션에 실제 전달하는 검증은 별도다. 확인한 CLI 버전: Codex 0.159.2, Claude Code 2.1.284 (2026-10-01).

## 빠른 시작: 공용 우편함

같은 OS 계정에서 두 에이전트가 같은 DB를 사용한다. 기본 저장소는 `~/.local/share/agent-bridge/mail.db`다. 메시지와 세션 ID는 코드 저장소에 들어가지 않는다.

```bash
cd /Users/firejune/Workspace/Firejune/agent-bridge
python3 bridge.py register naru --project blosharper --cwd /Users/firejune/Workspace/Firejune/blosharper
python3 bridge.py register roa --project blosharper --cwd /Users/firejune/Workspace/Firejune/blosharper
python3 bridge.py channel-bind modal-view --project blosharper --owner naru --description '모달 UI·반응형 담당'
python3 bridge.py channel-bind game-logic --project blosharper --owner roa --description '게임 규칙·상태 계약 담당'

# 둘 다 이 목록에서 담당자·역할·세션·작업 경로·세대 번호를 확인한다.
python3 bridge.py channels --project blosharper

# send는 저장만 하고 상대를 실행하지 않는다.
python3 bridge.py send --project blosharper --from-channel game-logic --channel modal-view \
  --key ui-request-001 --file - <<'MESSAGE'
체력 상태를 화면에 연결해 주세요.
작업 경로: 합의한 별도 워크트리
허용 파일: 합의한 UI 파일
완료 조건: 실제 화면에서 수치와 표시 일치 확인
MESSAGE

python3 bridge.py inbox --channel modal-view --project blosharper
python3 bridge.py show REQUEST_ID

# 상대의 현재 세션을 다시 찾지 않고, 요청에 고정된 답장 주소를 쓴다.
python3 bridge.py reply REQUEST_ID --key ui-response-001 --file - <<'MESSAGE'
구현 결과와 검증 근거를 여기에 적습니다.
MESSAGE
python3 bridge.py inbox --channel game-logic --project blosharper
```

`REQUEST_ID`에는 send 결과의 `id`를 쓴다. 같은 전송을 재시도할 때는 같은 `--key`를 사용한다. 같은 키로 본문이나 목적 채널을 바꾸면 거부한다. 채널 인계 후 같은 키로 재시도해도 최초 메시지를 반환하며 새 세션에 중복 전송하지 않는다.

직접 이름으로 보내는 `send --from roa --to naru`와 `inbox naru`도 지원한다. 여러 사령탑 대화가 있으면 양쪽 모두 채널을 지정하는 방식이 권장된다.

요청에 답변을 저장하면 요청은 `answered`가 된다. **answered는 답변이 있다는 뜻이며 구현 검증 완료를 뜻하지 않는다.** 답변 자체는 수신자 우편함에 남는다.

## 세션 교체와 인계

연결 정보 등록과 채널 연결은 별도다. 같은 사람이 여러 채널을 맡으면 각 채널은 연결 당시의 세션을 독립적으로 유지한다.

```bash
# 이 UUID는 예시 자리표시자다. 확인한 새 대화 ID로 대체한다.
python3 bridge.py register naru --project blosharper --transport codex \
  --session NEW_SESSION_UUID --cwd /Users/firejune/Workspace/Firejune/blosharper
python3 bridge.py channels --project blosharper
# 목록에서 확인한 현재 generation이 1인 경우. 경합으로 바뀌었으면 실패한다.
python3 bridge.py channel-bind modal-view --project blosharper --owner naru \
  --description '모달 UI·반응형 담당' --expect-generation 1
```

이제 새 요청은 세대 2의 새 세션으로 간다. **기존 요청의 목적지와 답장 주소는 세대 1을 유지한다.** 채널 이름이 같아도 이미 진행 중인 대화를 자동으로 새 세션에 옮기지 않는다. 이전 대화를 종료해야 한다면 미처리 요청을 확인하고 새 요청으로 명시적으로 인계한다.

```bash
# 채널 전체 미처리 목록: 이전 세대의 메시지도 session/generation과 함께 표시된다.
python3 bridge.py inbox --channel modal-view --project blosharper
# 현재 세션에 온 메시지만 보고 싶을 때
python3 bridge.py inbox --channel modal-view --project blosharper --session NEW_SESSION_UUID
# 종료 시 새 요청을 받지 않도록 닫기. 세대 번호는 최신 목록을 따른다.
python3 bridge.py channel-close modal-view --project blosharper --expect-generation 2
```

닫힌 채널은 새 요청을 받지 않는다. 기존 요청의 답변과 명시적 전달은 고정된 주소로 계속 가능하다. `channel-bind`로 현재 세대를 지정하면 다시 열 수 있다.

새로운 OS 계정이나 머신 사이의 네트워크 전달은 지원하지 않는다. SQLite 파일을 Git이나 클라우드 동기화 폴더로 공유하지 않는다.

## 기존 대화에 전달하기

기본 `inbox` 모드는 양쪽 모두 직접 우편함을 읽는다. 백그라운드 자동 감시나 알림은 없다. 각 에이전트의 작업 시작·종료 지점에서 inbox를 조회하는 방식으로 사용할 수 있다.

CLI 전달을 사용하려면 실제로 확인한 세션 UUID를 등록한다. 아래 ID는 자리표시자다.

```bash
python3 bridge.py register naru --project blosharper --transport codex \
  --session CODEX_THREAD_UUID --cwd /Users/firejune/Workspace/Firejune/blosharper
python3 bridge.py register roa --project blosharper --transport claude \
  --session CLAUDE_SESSION_UUID --cwd /Users/firejune/Workspace/Firejune/blosharper
python3 bridge.py routes --project blosharper

# 직접 이름으로 보내면 등록 후 새 메시지에 적용된다. 채널 전송은 channel-bind로 별도 갱신한다.
python3 bridge.py deliver MESSAGE_ID --dry-run
python3 bridge.py deliver MESSAGE_ID --timeout 180
```

- **Codex:** `codex queue --thread UUID --message TEXT`. 성공은 `submitted`이며 수신 측 실행이나 작업 완료를 확인한 것은 아니다. 상대가 우편함으로 답변해야 `answered`가 된다.
- **Claude:** `claude --resume UUID --model opus -p TEXT`. JSON 응답을 반대 방향의 메시지로 저장한다. 읽기 전용 `plan`, `Read,Grep,Glob` 도구로 제한한다. 오류·권한 거절을 정상 답변으로 처리하지 않는다.
- Claude `resume`은 실행 중인 대화에 알림을 주입하는 방식이 아니다. **쉬고 있는 전용 CLI 세션에만 사용한다.** 이미 작업 중인 Claude 대화는 `inbox` 모드로 협업한다. 다른 앱이나 직접 실행한 CLI의 활동은 브릿지가 감지하지 못한다.
- CLI가 PATH에 없다면 등록 시 `--executable /absolute/path/to/cli`를 지정한다.
- 메시지는 저장 당시의 경로·세션으로 고정된다. 이후 register로 대상을 바꿔도 기존 메시지가 다른 대화로 전달되지 않는다.
- 응답을 자동으로 다시 전달하지 않는다. 필요한 응답만 명시적으로 deliver하여 확인 메시지의 무한 왕복을 피한다.

## 전달 상태와 복구

| 상태 | 의미 |
| --- | --- |
| `pending` | 우편함에 저장됨, CLI 전달 전 |
| `dispatching` | 해당 메시지를 한 전달기가 선점함 |
| `submitted` | Codex CLI가 성공을 반환함 |
| `answered` | 연결된 답변이 저장됨 |
| `failed` | 프로세스 실행 자체에 실패함 |
| `uncertain` | 타임아웃·비정상 종료·결과 해석 실패로 수신 여부를 확정할 수 없음 |
| `done` | 확인 후 수동으로 닫음 |

SQLite 트랜잭션으로 같은 메시지의 동시 전송을 막는다. 동일 CLI 세션에 `dispatching` 또는 `uncertain` 메시지가 있으면 다른 메시지도 전달하지 않는다. 프로세스가 강제 종료되어 `dispatching`에 남으면 직접 확인해야 한다.

외부 CLI 전달과 DB 기록을 하나의 트랜잭션으로 묶을 수 없으므로 **정확히 한 번 실행을 보장하지 않는다.** 애매한 결과를 자동 재전송하지 않아 중복 실행 위험을 줄인다. CLI 실행 성공 직후 브릿지가 종료된 경우에도 다음 절차를 따른다.

```bash
python3 bridge.py show MESSAGE_ID
# 수신 측 대화와 프로세스를 확인한 후, 근거를 남겨 상태를 결정한다.
python3 bridge.py resolve MESSAGE_ID --state submitted --note '수신 대화에서 메시지 확인'
# 전달되지 않았고 기존 실행도 끝났음을 확인했을 때만 재전송 가능 상태로 변경한다.
python3 bridge.py resolve MESSAGE_ID --state pending --note '기존 실행 종료 및 미수신 확인'
# 처리가 끝난 메시지를 닫는다.
python3 bridge.py resolve MESSAGE_ID --state done --note '결과 확인 완료'
```

`show`에는 상태 변경 이력이 포함된다. `inbox --all`로 닫힌 메시지도 조회한다. CLI는 전송 오류나 애매한 결과에 종료 코드 1을 반환한다.

## 협업 범위

브릿지는 운반과 기록을 담당한다. 동료 메시지를 새로운 사용자 승인으로 취급하지 않는다. 등록된 이름은 로컬 라벨이며 발신자 인증 수단이 아니다. 같은 OS 계정을 신뢰하는 도구다.

작업 요청에는 목적, 프로젝트·워크트리, 허용 파일, 완료 조건을 넣는다. 실제 파일 잠금이나 Git 병합은 수행하지 않으므로 두 에이전트가 같은 파일을 동시에 수정하지 않도록 범위를 합의해야 한다. 메시지 DB에는 본문이 평문으로 저장되므로 크리덴셜을 보내지 않는다.

## 검증

```bash
python3 -m unittest discover -s tests -v
```

테스트는 임시 DB에서 실제 CLI로 우편함 왕복을 실행하고, 가짜 CLI 프로세스로 Codex·Claude 어댑터를 검증한다. 중복키·동시 선점·채널 탐색·인계 경합·양쪽 주소 고정·셸 문자 보존·타임아웃·오류·응답 연결·초기 스키마 마이그레이션을 확인한다. 세션 탐색 테스트는 페이지 처리·공유 서버 실패 시 전환·실시간 상태 구분·경로 검증·부분 실패도 확인한다. 실제 AI 세션 사이의 전송·자동 기동은 이 테스트의 검증 범위가 아니다.
