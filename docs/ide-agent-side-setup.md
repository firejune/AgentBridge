---
name: reply_to_chloe
description: 클로이(OpenClaw)에게 메시지를 전송합니다. 작업 지시에 대한 응답, 진행 보고, 질문 전달 시 사용합니다.
---

# Reply to Chloe (클로이에게 응답)

클로이(OpenClaw)가 `[CHLOE_BRIDGE]` 헤더를 포함한 작업 지시를 보내왔을 때, 그 안의 `session_id` 정보를 사용하여 응답을 보내는 스킬입니다.

> **필수 규칙**: `[CHLOE_BRIDGE]` 헤더가 포함된 메시지를 받아 작업을 수행한 경우, **작업 완료 후 반드시 이 스킬을 사용하여 클로이에게 결과를 리포트해야 합니다.** 리포트 없이 작업을 종료하지 마세요.

## 언제 사용하는가

1. **클로이의 작업 지시를 받았을 때** — 메시지에 `[CHLOE_BRIDGE]` 헤더가 있으면 클로이가 보낸 것
2. **작업 완료 보고 (필수)** — 요청된 작업을 마친 후 결과를 반드시 전달
3. **진행 상황 보고** — 긴 작업(1분 이상)의 중간 경과
4. **질문/확인 필요** — 작업 수행 중 판단이 필요한 경우

## 메시지 수신 헤더 파싱

클로이가 보낸 메시지는 아래 형태의 헤더를 포함합니다:

```
[CHLOE_BRIDGE]
session_id=5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5
request_id=abc123-uuid
[/CHLOE_BRIDGE]

실제 작업 지시 내용
```

이 헤더에서 `session_id`, `request_id`를 추출하여 응답에 사용합니다.

## 핵심 명령어

```bash
openclaw agent --session-id '<SESSION_UUID>' -m '<메시지>'
```

> `openclaw message send`가 아닙니다. `openclaw agent --session-id`를 사용해야 합니다.

## 사용 방법

### 셸 스크립트 실행
```bash
./scripts/reply-to-chloe.sh \
  --session-id '5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5' \
  --request-id 'abc123-uuid' \
  --message '작업 완료했어요. n8n 서비스 정상 동작 중입니다.'
```

### 터미널에서 직접 (스크립트 없이)
```bash
openclaw agent \
  --session-id '5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5' \
  -m '루나입니다. 작업 완료 보고드려요.'
```

### dry-run (전송 없이 확인)
```bash
./scripts/reply-to-chloe.sh \
  --session-id '5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5' \
  --message '테스트' \
  --dry-run
```

## 파라미터

| 파라미터 | 필수 | 설명 |
|----------|------|------|
| `--session-id` | ✅ | 클로이의 세션 UUID (`CHLOE_BRIDGE` 헤더에서 추출) |
| `--message` / `-m` | ✅ | 전달할 메시지 내용 |
| `--request-id` | ❌ | 원본 요청 ID (추적용, 메시지에 자동 포함) |
| `--dry-run` | ❌ | 전송 없이 페이로드 확인 |

## 응답 시점 가이드

| 작업 유형 | 응답 시점 |
|-----------|-----------|
| 짧은 작업 (10초 이내) | 최종 완료 보고만 |
| 긴 작업 (1분 이상) | ① 작업 시작 → ② 진행 중 → ③ 작업 완료 |

### 응답 메시지 예시

**작업 시작**
```bash
./scripts/reply-to-chloe.sh \
  --session-id '5671c52b-...' \
  --request-id 'abc123' \
  --message '작업 시작합니다. n8n 서비스 점검 진행할게요.'
```

**완료 보고**
```bash
./scripts/reply-to-chloe.sh \
  --session-id '5671c52b-...' \
  --request-id 'abc123' \
  --message '작업 완료. 모든 서비스 정상입니다.'
```

## 에러 처리

- `openclaw` CLI 미설치 → 에러 메시지 출력 + 종료코드 1
- 전송 실패 → 3초 대기 후 1회 재시도
- 최종 실패 → 에러 로그 기록 + 종료코드 1
- 로그 위치: `scripts/reply-chloe.log`

## 주의 사항

- **싱글쿼트(`'`) 사용 필수**: 파라미터 값은 반드시 싱글쿼트로 감쌀 것. 더블쿼트(`"`)를 사용하면 `!`가 셸 history expansion을 트리거하여 명령이 깨짐
  ```bash
  # ✅ 올바른 사용
  --message '작업 완료했어요!' --session-id '5671c52b-...'
  
  # ❌ 잘못된 사용 (!" 패턴이 history expansion 트리거)
  --message "작업 완료했어요!" --session-id "5671c52b-..."
  ```
- `--session-id`는 클로이가 `[CHLOE_BRIDGE]` 헤더에서 알려준 UUID를 그대로 사용 — 임의로 변경하면 메시지 유실
