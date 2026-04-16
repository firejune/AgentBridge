# Antigravity Internal Commands Reference

이 문서는 IDE 내부에서 `chat`, `antigravity`, `agent` 키워드를 포함하여 활성화된 내부 커맨드 목록을 분류하여 정리한 레퍼런스입니다. (AgentBridge 백도어를 통해 추출됨)

확장 프로그램이나 외부 스크립트에서 자동화를 구축할 때 `vscode.commands.executeCommand('command_name', ...args)` 형태로 응용할 수 있습니다.

## 🤖 1. 에이전트 및 채팅 조작 (Core Agent & Chat)
에이전트와의 직접적인 상호작용 및 채팅창 제어와 관련된 커맨드입니다.

- `antigravity.openAgent`: 에이전트 패널을 엽니다.
- `antigravity.sendPromptToAgentPanel`: (AgentBridge의 핵심) 에이전트 패널로 프롬프트를 조용히 주입합니다.
- `antigravity.toggleChatFocus`: 채팅창 인풋으로 포커스를 이동시킵니다.
- `antigravity.startNewConversation`: 새로운 대화 세션을 시작합니다.
- `antigravity.switchBetweenWorkspaceAndAgent`: 워크스페이스와 에이전트 간 포커스를 전환합니다.
- `antigravity.rejectAgentStep`: 에이전트의 현재 작업 단계를 거부/취소합니다.
- `antigravity.trackBackgroundConversationCreated`: 백그라운드 대화 세션 생성 이벤트를 기록합니다.
- `antigravity.reloadAgentSidePanel`: 에이전트 사이드 패널을 새로고침합니다.
- `antigravity.openConversationPicker`: 이전 대화 목록 선택기(Picker)를 엽니다.
- `antigravity.openConversationWorkspaceQuickPick`: 워크스페이스 대화 Quick Pick을 엽니다.

## 💻 2. 터미널 및 콘솔 연동 (Terminal & Shell)
에이전트가 터미널을 읽거나 명령을 내리는 것과 관련된 커맨드입니다.

- `antigravity.readTerminal`: 활성화된 터미널의 내용을 읽어 들입니다.
- `antigravity.sendTerminalToSidePanel`: 현재 터미널의 내용을 사이드 패널(채팅창)로 보냅니다.
- `antigravity.onManagerTerminalCommandStart`: 터미널 명령어 실행 시작 이벤트.
- `antigravity.onManagerTerminalCommandData`: 터미널 명령어 데이터 수신 이벤트.
- `antigravity.onManagerTerminalCommandFinish`: 터미널 명령어 실행 완료 이벤트.
- `antigravity.onShellCommandCompletion`: 쉘(Shell) 명령어 자동완성 처리.
- `antigravity.showManagedTerminal`: 매니지드 터미널을 표시합니다.
- `antigravity.updateTerminalLastCommand`: 터미널의 마지막 명령어 상태를 업데이트합니다.

## 📝 3. 코드 액션 및 컨텍스트 제어 (Code Action & Context)
특정 코드 블록, 파일, 심볼에 대한 에이전트 동작입니다.

- `antigravity.prioritized.explainProblem`: 선택된 코드 에러/문제를 설명합니다.
- `antigravity.prioritized.agentAcceptAllInFile`: 파일 내의 에이전트 제안을 모두 수락합니다.
- `antigravity.prioritized.agentRejectAllInFile`: 파일 내의 에이전트 제안을 모두 거부합니다.
- `antigravity.prioritized.agentAcceptFocusedHunk`: 포커스된 코드 헝크(Hunk)만 수락합니다.
- `antigravity.prioritized.agentRejectFocusedHunk`: 포커스된 코드 헝크(Hunk)만 거부합니다.
- `antigravity.prioritized.agentFocusNextHunk` / `agentFocusPreviousHunk`: 제안된 다음/이전 헝크로 이동.
- `antigravity.prioritized.agentFocusNextFile` / `agentFocusPreviousFile`: 제안된 다음/이전 파일로 이동.
- `antigravity.prioritized.supercompleteAccept`: 슈퍼 자동완성(Supercomplete) 수락.
- `antigravity.prioritized.supercompleteEscape`: 슈퍼 자동완성(Supercomplete) 취소.
- `antigravity.snoozeAutocomplete`: 자동완성 일시 정지(Snooze).

## 🛠️ 4. 시스템 및 패널 UI 제어 (UI & Management)
IDE 패널 열기, 설정, 개발자 도구 등 UI 상태 제어입니다.

- `antigravity.toggleModelSelector`: LLM 모델 선택기를 토글합니다.
- `antigravity.togglePlanningModeSelector`: 플래닝 모드(Planning Mode) 설정기를 토글합니다.
- `antigravity.openQuickSettingsPanel`: 빠른 설정 패널을 엽니다.
- `antigravity.openCustomizationsTab`: 커스터마이징 탭을 엽니다.
- `antigravity.toggleManagerDevTools`: 매니저(에이전트) 전용 개발자 도구를 엽니다.
- `antigravity.toggleSettingsDevTools`: 설정 전용 개발자 도구를 엽니다.
- `antigravity.openReviewChanges`: 변경 사항 리뷰(Review Changes) 창을 엽니다.
- `antigravity.openDiffZones` / `antigravity.setDiffZonesState` / `antigravity.sidecar.sendDiffZone`: Diff UI 상태 제어.
- `antigravity.showBrowserAllowlist`: 브라우저 동작 허용 목록(Allowlist) 표시.
- `antigravity.openDocs` / `antigravity.openMcpDocsPage` / `antigravity.openRulesEducationalLink`: 공식 문서 및 룰 가이드 열기.
- `antigravity.startDemoMode`: 데모 모드를 시작합니다.
- `antigravity.reloadWindow`: (내장 명령 래핑) 창을 새로고침합니다.

## 🎤 5. 멀티모달 & 음성 기능 (Multimodal & Voice)
- `antigravity.startVoiceRecording`: 음성 입력 녹음을 시작합니다.
- `antigravity.stopVoiceRecording`: 음성 입력 녹음을 중지합니다.
- `antigravity.playAudio` / `antigravity.playNote`: 오디오 또는 효과음을 재생합니다.

## 🐛 6. 오류 보고 및 복구 (Error & State Reporting)
- `antigravity.uploadErrorAction`: 오류 로그를 업로드합니다.
- `antigravity.simulateSegFault`: (개발/테스트용) SegFault 익셉션을 시뮬레이션합니다.
- `antigravity.showLanguageServerCrashFullScreenView`: LS(Language Server) 크래시 전체 화면 표시.
- `antigravity.restartLanguageServer` / `antigravity.restartMainLanguageServer`: 언어 서버 재시작.
- `antigravity.resetOnboardingBackend` / `antigravity.onboarding.reset` / `antigravity.manager.onboarding.reset`: 백엔드/온보딩 상태 초기화.

## 원본 커맨드 덤프
이 목록 외에도 VS Code 네이티브 `workbench.action.chat...` 커맨드와 `chatEditing...` 커맨드들이 포함되어 있습니다. 전체 목록은 언제든지 `/debug/commands` 엔드포인트를 호출하여 직접 확인하실 수 있습니다.
