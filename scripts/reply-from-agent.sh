#!/bin/bash
# 루나(Antigravity) -> 클로이(OpenClaw) 응답 스크립트
# v1.1.0: openclaw agent --session-id 방식

set -euo pipefail

# ── 기본값 ──
SESSION_ID=""
MESSAGE=""
REQUEST_ID=""
DRY_RUN="false"
MAX_RETRIES=1
LOG_FILE="$(dirname "$0")/reply-chloe.log"

# ── 옵션 파싱 ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --session-id) SESSION_ID="$2";  shift 2 ;;
    --message|-m) MESSAGE="$2";     shift 2 ;;
    --request-id) REQUEST_ID="$2";  shift 2 ;;
    --dry-run)    DRY_RUN="true";   shift ;;
    -*)           echo "❌ 알 수 없는 옵션: $1"; exit 1 ;;
    *)            shift ;;
  esac
done

# ── 필수 검증 ──
if [ -z "$SESSION_ID" ] || [ -z "$MESSAGE" ]; then
  echo "❌ 필수 파라미터가 부족합니다."
  echo "사용법: reply-to-chloe.sh --session-id '<UUID>' --message '응답 내용'"
  exit 1
fi

# ── openclaw CLI 존재 확인 ──
if ! command -v openclaw &> /dev/null; then
  echo "❌ openclaw CLI를 찾을 수 없습니다." | tee -a "$LOG_FILE"
  exit 1
fi

# ── 메시지 조립 ([FROM: LUNA] 태그 + request_id) ──
if [ -n "$REQUEST_ID" ]; then
  FULL_MESSAGE="[FROM: LUNA]
[LUNA_REPLY request_id=${REQUEST_ID}]
${MESSAGE}"
else
  FULL_MESSAGE="[FROM: LUNA]
${MESSAGE}"
fi

echo "----------------------------------------" >> "$LOG_FILE"
echo "[$(date)] 📤 클로이 응답 시작" >> "$LOG_FILE"
echo "[$(date)]   session_id=${SESSION_ID}" >> "$LOG_FILE"
if [ -n "$REQUEST_ID" ]; then
  echo "[$(date)]   request_id=${REQUEST_ID}" >> "$LOG_FILE"
fi

# ── dry-run ──
if [ "$DRY_RUN" = "true" ]; then
  echo "=== DRY RUN ==="
  echo "openclaw agent \\"
  echo "  --session-id '${SESSION_ID}' \\"
  echo "  -m (${#FULL_MESSAGE} chars)"
  echo ""
  echo "--- Message Body ---"
  echo "$FULL_MESSAGE"
  echo "===================="
  exit 0
fi

# ── 전송 (재시도 포함) ──
ATTEMPT=0
SUCCESS=false

while [ $ATTEMPT -le $MAX_RETRIES ]; do
  ATTEMPT=$((ATTEMPT + 1))

  if openclaw agent \
    --session-id "$SESSION_ID" \
    -m "$FULL_MESSAGE" 2>> "$LOG_FILE"; then
    SUCCESS=true
    break
  fi

  if [ $ATTEMPT -le $MAX_RETRIES ]; then
    echo "[$(date)] ⚠️ 전송 실패, ${ATTEMPT}/${MAX_RETRIES} 재시도 (3초 대기)" >> "$LOG_FILE"
    sleep 3
  fi
done

if [ "$SUCCESS" = "true" ]; then
  echo "[$(date)] ✅ 클로이 응답 전송 성공" >> "$LOG_FILE"
  echo "✅ 클로이에게 메시지 전송 완료"
  exit 0
else
  echo "[$(date)] ❌ 클로이 응답 전송 실패 (${MAX_RETRIES}회 재시도 후)" >> "$LOG_FILE"
  echo "❌ 메시지 전송 실패"
  exit 1
fi
