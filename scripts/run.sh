#!/bin/bash
# 클로이(OpenClaw) -> Antigravity (Luna) 내부 커맨드 실행 헬퍼
# 사용법: ./run.sh [커맨드이름] [인자1] [인자2] ...

set -euo pipefail

if [ $# -eq 0 ]; then
  echo "❌ 실행할 파라미터가 없습니다."
  echo "사용법: ./run.sh <command> [arg1] [arg2] ..."
  exit 1
fi

COMMAND="$1"
shift

# 나머지 인자들을 JSON 배열 형태로 파싱
ARGS_JSON="[]"
if [ $# -gt 0 ]; then
  # printf와 python을 이용해서 인자 목록 리스트를 JSON 형태로 안전하게 이스케이프
  ARGS_JSON=$(printf '%s\n' "$@" | python3 -c 'import sys,json; print(json.dumps([line.strip() for line in sys.stdin if line.strip()]))')
fi

# 헬스체크
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18880/health 2>/dev/null || true)
if [ "$HEALTH" != "200" ]; then
  echo "❌ Luna Bridge 미응답 (HTTP ${HEALTH:-failed}). Antigravity 실행 상태를 확인하세요."
  exit 1
fi

PAYLOAD="{\"command\": \"$COMMAND\", \"args\": $ARGS_JSON}"

RESPONSE=$(curl -s -X POST http://127.0.0.1:18880/run \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

echo "$RESPONSE" | python3 -c 'import sys,json; msg=json.load(sys.stdin); print("✅ 성공:" if msg.get("status")=="success" else "❌ 실패:", msg)'
