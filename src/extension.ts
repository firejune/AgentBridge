import * as vscode from 'vscode';
import * as http from 'http';

let server: http.Server | undefined;
let lastStatus: { time: string; ok: boolean; detail: string } = {
    time: new Date().toISOString(),
    ok: false,
    detail: 'Bridge not yet triggered.'
};

export function activate(context: vscode.ExtensionContext) {
    console.log('🚀 Luna Antigravity Bridge v1.2.0 activated (Silent API mode)');

    server = http.createServer((req, res) => {
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Content-Type', 'application/json');

        if (req.method === 'GET' && req.url === '/status') {
            res.writeHead(200);
            return res.end(JSON.stringify({ status: 'ok', bridge: lastStatus }));
        }

        if (req.method === 'GET' && req.url === '/health') {
            res.writeHead(200);
            return res.end(JSON.stringify({ status: 'ok', version: '1.2.0' }));
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

        if (req.method === 'POST' && req.url === '/run') {
            let body = '';
            req.on('data', chunk => { body += chunk.toString(); });
            req.on('end', async () => {
                try {
                    const data = JSON.parse(body);
                    const cmd = data.command;
                    const args = data.args || [];
                    
                    if (!cmd) {
                        res.writeHead(400);
                        return res.end(JSON.stringify({ status: 'error', message: 'No command provided (requires "command" field).' }));
                    }

                    console.log(`[Luna Bridge] Executing dynamic command: ${cmd} with args:`, args);
                    const result = await vscode.commands.executeCommand(cmd, ...args);
                    
                    res.writeHead(200);
                    res.end(JSON.stringify({ status: 'success', command: cmd, result: result || null }));
                } catch (e) {
                    res.writeHead(500);
                    res.end(JSON.stringify({ status: 'error', error: String(e) }));
                }
            });
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

                    if (!msg) {
                        res.writeHead(400);
                        return res.end(JSON.stringify({ status: 'error', message: 'No message provided.' }));
                    }

                    res.writeHead(200);
                    res.end(JSON.stringify({ status: 'success', message: 'Push Event 성공! 루나 API 주입 완료.' }));

                    // Fire and Forget 주입
                    (async () => {
                        try {
                            await vscode.commands.executeCommand('antigravity.startNewConversation');
                            await vscode.commands.executeCommand('antigravity.sendPromptToAgentPanel', msg);
                            lastStatus = {
                                time: new Date().toISOString(),
                                ok: true,
                                detail: `API Injected | ${msg.substring(0, 80)}`
                            };
                            console.log(`✅ Luna Bridge: API Injected successfully`);
                        } catch (e) {
                            lastStatus = {
                                time: new Date().toISOString(),
                                ok: false,
                                detail: `Failed API Injection: ${String(e)}`
                            };
                            console.error('❌ Luna Bridge API injection failed:', e);
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
