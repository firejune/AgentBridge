import * as vscode from 'vscode';
import * as http from 'http';

let server: http.Server | undefined;
let lastStatus: { time: string; ok: boolean; detail: string } = {
    time: new Date().toISOString(),
    ok: false,
    detail: 'Bridge not yet triggered.'
};

async function injectMessage(msg: string, _autoSubmit: boolean): Promise<string> {
    const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

    // 1. 에이전트/채팅 패널 열기
    try {
        await vscode.commands.executeCommand('antigravity.openAgent');
        await sleep(400);
    } catch (e) {
        console.warn('[Luna] openAgent failed:', e);
    }

    // 2. 채팅 인풋에 포커스
    try {
        await vscode.commands.executeCommand('antigravity.toggleChatFocus');
        await sleep(200);
    } catch {
        try {
            await vscode.commands.executeCommand('workbench.panel.chat.view.copilot.focus');
            await sleep(200);
        } catch (e2) {
            console.warn('[Luna] focus fallback failed:', e2);
        }
    }

    // 3. 클립보드에 메시지 복사
    await vscode.env.clipboard.writeText(msg);

    return 'ready: panel opened, focused, clipboard set';
}

export function activate(context: vscode.ExtensionContext) {
    console.log('🚀 Luna Antigravity Bridge v1.1.0 activated');

    server = http.createServer((req, res) => {
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Content-Type', 'application/json');

        if (req.method === 'GET' && req.url === '/status') {
            res.writeHead(200);
            return res.end(JSON.stringify({ status: 'ok', bridge: lastStatus }));
        }

        if (req.method === 'GET' && req.url === '/health') {
            res.writeHead(200);
            return res.end(JSON.stringify({ status: 'ok', version: '1.1.0' }));
        }

        // 디버그: 사용 가능한 chat/antigravity 커맨드 목록
        if (req.method === 'GET' && req.url === '/debug/commands') {
            (async () => {
                try {
                    const cmds = await vscode.commands.getCommands(true);
                    const chatCmds = cmds.filter(c => 
                        c.includes('chat') || c.includes('antigravity') || c.includes('agent')
                    ).sort();
                    res.writeHead(200);
                    res.end(JSON.stringify({ count: chatCmds.length, commands: chatCmds }));
                } catch (e) {
                    res.writeHead(500);
                    res.end(JSON.stringify({ error: String(e) }));
                }
            })();
            return;
        }

        // 채팅 인풋 포커스 전용 (앱 활성화 후 호출)
        if (req.method === 'POST' && req.url === '/focus') {
            (async () => {
                try {
                    await vscode.commands.executeCommand('antigravity.openAgent');
                    await new Promise(r => setTimeout(r, 300));
                    await vscode.commands.executeCommand('antigravity.toggleChatFocus');
                    res.writeHead(200);
                    res.end(JSON.stringify({ status: 'ok', focused: true }));
                } catch (e) {
                    res.writeHead(500);
                    res.end(JSON.stringify({ status: 'error', error: String(e) }));
                }
            })();
            return;
        }

        if (req.method === 'POST' && req.url === '/trigger') {
            let body = '';
            req.on('data', chunk => {
                body += chunk.toString();
            });
            req.on('end', async () => {
                try {
                    const data = JSON.parse(body);
                    const msg = data.msg || data.message;
                    const autoSubmit = data.autoSubmit !== false;

                    if (!msg) {
                        res.writeHead(400);
                        return res.end(JSON.stringify({ status: 'error', message: 'No message provided.' }));
                    }

                    res.writeHead(200);
                    res.end(JSON.stringify({ status: 'success', message: 'Push Event 성공! 루나 소환 완료.' }));

                    // Fire and Forget
                    (async () => {
                        await new Promise(resolve => setTimeout(resolve, 200));
                        try {
                            const method = await injectMessage(msg, autoSubmit);
                            lastStatus = {
                                time: new Date().toISOString(),
                                ok: true,
                                detail: `${method} | autoSubmit:${autoSubmit} | ${msg.substring(0, 80)}`
                            };
                            console.log(`✅ Luna Bridge: ${method}`);
                        } catch (e) {
                            lastStatus = {
                                time: new Date().toISOString(),
                                ok: false,
                                detail: `Failed: ${String(e)}`
                            };
                            console.error('❌ Luna Bridge injection failed:', e);
                        }
                    })();
                } catch (e) {
                    if (!res.writableEnded) {
                        res.writeHead(500);
                        res.end(JSON.stringify({ status: 'error', error: String(e) }));
                    }
                }
            });
        } else if (req.method !== 'GET') {
            res.writeHead(404);
            res.end(JSON.stringify({ status: 'not_found' }));
        }
    });

    server.on('error', (err: NodeJS.ErrnoException) => {
        if (err.code === 'EADDRINUSE') {
            const msg = `Luna Bridge: 포트 18880이 이미 사용 중입니다. lsof -i :18880 으로 확인 후 kill 해주세요.`;
            console.error(`❌ ${msg}`);
            vscode.window.showErrorMessage(msg, '디버그 터미널 열기').then(sel => {
                if (sel) {
                    const t = vscode.window.createTerminal('Luna Bridge Debug');
                    t.show();
                    t.sendText('lsof -i :18880');
                }
            });
        } else {
            vscode.window.showErrorMessage(`Luna Bridge 서버 오류: ${err.message}`);
        }
    });

    server.listen(18880, '127.0.0.1', () => {
        console.log('📡 Luna Bridge listening on http://127.0.0.1:18880');
    });

    context.subscriptions.push({
        dispose: () => {
            if (server) {
                server.close();
            }
        }
    });
}

export function deactivate() {
    if (server) {
        server.close();
    }
}
