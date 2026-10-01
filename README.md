# Agent Bridge

Codex·Claude Code 사령탑의 세션을 조회하고 로컬 소켓으로 실시간 질문·답변을 주고받는 도구. Python 표준 라이브러리만 사용하며 상태는 메모리에 유지한다.

- [스킬 진입점](skill/agent-bridge/SKILL.md)
- [명령·운영 사용법](skill/agent-bridge/references/usage.md)
- 실행 코드 정본: `skill/agent-bridge/scripts/`

루트의 `bridge.py`, `broker.py`, `sessions.py`는 기존 명령 호환을 위한 심볼릭 링크다. 실제 코드는 스킬 폴더 안에 한 벌만 유지한다.

## 전역 설치

이 머신에서는 아래 세 경로를 동일한 `skill/agent-bridge` 폴더로 연결했다.

- Codex: `~/.codex/skills/agent-bridge`
- 공용 탐색·실행 경로: `~/.agents/skills/agent-bridge`
- Claude Code: `~/.claude/skills/agent-bridge`

Codex에서는 `$agent-bridge`, Claude Code에서는 `/agent-bridge`로 호출할 수 있다. 별도 실행 권한이나 자동 발신 권한을 추가하지 않는다. 스킬만 다른 곳에 설치할 때도 폴더 안의 스크립트와 참조 문서를 함께 유지한다.

```bash
python3 ~/.agents/skills/agent-bridge/scripts/bridge.py --help
python3 ~/.agents/skills/agent-bridge/scripts/bridge.py sessions --provider claude
```

심볼릭 링크 설치이므로 저장소 위치를 옮기면 세 링크도 갱신해야 한다. 브로커를 재시작하면 메모리 상태가 사라진다. 전역 스킬 설치가 브로커를 자동 기동하거나 AI 대화를 깨우지는 않는다.

## 개발·검증

```bash
python3 -m unittest discover -s tests -v
```

테스트는 실제 Unix 소켓과 별도 CLI 프로세스로 협업 흐름을 확인한다. 코드 수정은 스킬 폴더의 정본에서 하며 스크립트를 따로 복제하지 않는다.
