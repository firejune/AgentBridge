---
name: notify_antigravity
description: 안티그래비티(루나) 채팅창에 메시지를 자동 전송하여 작업을 위임합니다. 라우팅 헤더를 포함하여 루나가 작업 완료 후 클로이에게 응답할 수 있습니다.
---

# Notify Antigravity (루나 깨우기) v2.1

클로이(OpenClaw)가 루나(Antigravity AI)에게 메시지를 전송하고 자동 실행시킬 때 사용합니다.
메시지에 `[CHLOE_BRIDGE]` 라우팅 헤더가 자동 삽입되어, 루나가 작업 완료 후 올바른 세션으로 응답할 수 있습니다.

## 사용 방법

### 기본 (자동 제출)
```bash
./scripts/wake-up-luna.sh '루나 언니, 시스템 상태를 점검해 주세요.' \
  --session-id '5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5'
```

### 수동 제출 대기
```bash
./scripts/wake-up-luna.sh '확인이 필요한 메시지입니다.' \
  --session-id '5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5' \
  --no-submit
```

### request-id 직접 지정
```bash
./scripts/wake-up-luna.sh '배포 확인 부탁해요.' \
  --session-id '5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5' \
  --request-id 'deploy-check-001'
```

### dry-run (전송 없이 메시지 포맷 확인)
```bash
./scripts/wake-up-luna.sh '테스트' \
  --session-id '5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5' \
  --dry-run
```

## 파라미터

| 파라미터 | 필수 | 설명 |
|----------|------|------|
| 메시지 (positional) | ✅ | 루나에게 전달할 작업 지시 |
| `--session-id` | ✅ | 클로이의 현재 세션 UUID (응답 수신용) |
| `--request-id` | ❌ | 요청 추적 ID (미지정 시 UUID 자동 생성) |
| `--no-submit` | ❌ | 붙여넣기만 하고 Enter 전송 안 함 |
| `--dry-run` | ❌ | 전송 없이 조립된 메시지 출력 |

## 메시지 포맷

스크립트가 자동으로 아래 형태의 라우팅 헤더를 메시지 앞에 삽입합니다:

```
[CHLOE_BRIDGE]
session_id=5671c52b-d1e2-4cc2-9f5a-cfdfa3fe9bc5
request_id=abc123-uuid
[/CHLOE_BRIDGE]

실제 작업 지시 내용
```

루나는 이 헤더를 파싱하여 발신자를 식별하고, 작업 완료 후 `reply-to-chloe` 스킬로 같은 `session_id`에 응답합니다.

## 상태 확인
```bash
curl http://127.0.0.1:18880/health
curl http://127.0.0.1:18880/status
```

## 주의 사항

- **Antigravity IDE가 반드시 실행 중이어야 합니다**
- **외부 프로세스(Terminal.app 등)에서 실행해야 합니다** — Antigravity 내장 터미널에서는 osascript 샌드박싱으로 동작하지 않음
- `--session-id`는 필수 — 루나가 응답할 세션을 반드시 명시해야 합니다
- 첫 실행 시 Accessibility 권한 허용 팝업이 뜰 수 있음
