#!/bin/bash
# 클로이(OpenClaw) -> Antigravity (Luna) 브릿지 스크립트
# v2.1.0: session_id 기반 CHLOE_BRIDGE 헤더

set -euo pipefail

# ── 기본값 ──
SESSION_ID=""
REQUEST_ID=""
DRY_RUN="false"
MESSAGE=""
LOG_FILE="$(dirname "$0")/luna-skill.log"

# ── 옵션 파싱 ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --session-id) SESSION_ID="$2";  shift 2 ;;
    --request-id) REQUEST_ID="$2";  shift 2 ;;
    --dry-run)    DRY_RUN="true";   shift ;;
    -*)           echo "❌ 알 수 없는 옵션: $1"; exit 1 ;;
    *)
      if [ -z "$MESSAGE" ]; then
        MESSAGE="$1"
      else
        MESSAGE="$MESSAGE $1"
      fi
      shift
      ;;
  esac
done

echo "----------------------------------------" >> "$LOG_FILE"
echo "[$(date)] 🚀 브릿지 실행 (v2.1)" >> "$LOG_FILE"

if [ -z "$MESSAGE" ]; then
  echo "❌ 메시지가 필요합니다."
  echo "사용법: wake-up-luna.sh '메시지' --session-id '<UUID>'"
  exit 1
fi

if [ -z "$SESSION_ID" ]; then
  echo "❌ --session-id는 필수입니다."
  echo "사용법: wake-up-luna.sh '메시지' --session-id '<UUID>'"
  exit 1
fi

# request_id 미지정 시 자동 생성
if [ -z "$REQUEST_ID" ]; then
  REQUEST_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
fi

# ── 라우팅 헤더 조립 ──
BRIDGED_MESSAGE="[CHLOE_BRIDGE]
session_id=${SESSION_ID}
request_id=${REQUEST_ID}
[/CHLOE_BRIDGE]

${MESSAGE}"

echo "[$(date)] 📨 request_id=${REQUEST_ID} session_id=${SESSION_ID}" >> "$LOG_FILE"

# ── dry-run ──
if [ "$DRY_RUN" = "true" ]; then
  echo "=== DRY RUN ==="
  echo "$BRIDGED_MESSAGE"
  echo "================"
  exit 0
fi

# ── 헬스체크 ──
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18880/health 2>/dev/null)
if [ "$HEALTH" != "200" ]; then
  echo "❌ Luna Bridge 미응답 (HTTP ${HEALTH}). Antigravity 확인 필요."
  exit 1
fi

# ── JSON 이스케이프 ──
ESCAPED_MSG=$(printf '%s' "$BRIDGED_MESSAGE" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')

# Step 1: 루나 에이전트로 API 주입
curl -s -X POST http://127.0.0.1:18880/trigger \
  -H "Content-Type: application/json" \
  -d "{\"msg\":${ESCAPED_MSG}}" > /dev/null

echo "[$(date)] 💉 내부 API 주입 완료" >> "$LOG_FILE"

echo "✅ 루나 브릿지 완료 (request_id: ${REQUEST_ID})"
exit 0
