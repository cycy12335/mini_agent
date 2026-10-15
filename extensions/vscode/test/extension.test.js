'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { EventEmitter } = require('node:events');
const crypto = require('node:crypto');

test('extension host connects approval, native Diff and cooperative cancellation', async () => {
  const storage = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-host-'));
  const messages = [];
  let open, receive, disposePanel, launched, panel, provider, viewProvider;
  const commands = [];
  const saved = new Map();
  const secrets = new Map();
  class FakeProcess extends EventEmitter {
    constructor(python, args, options) { super(); Object.assign(this, { python, args, options }); launched = this; }
    approve(id, approved) { this.decision = { id, approved }; }
    send(command) { (this.sent ||= []).push(command); }
    cancel() { this.cancelled = true; }
    dispose() { this.cancel(); }
  }
  const disposable = () => ({ dispose() {} });
  const uri = file => ({ fsPath: file, scheme: 'file', toString() { return 'resource:///' + file.replaceAll('\\', '/'); } });
  const context = { subscriptions: [], secrets: { get: async key => secrets.get(key), store: async (key, value) => secrets.set(key, value), delete: async key => secrets.delete(key) }, globalState: { get: key => saved.get(key), update: async (key, value) => saved.set(key, value) }, globalStorageUri: uri(storage), extensionUri: uri(path.resolve(__dirname, '..')) };
  const vscode = {
    Uri: { file: uri, parse: value => ({ toString: () => value }), joinPath: (base, ...parts) => uri(path.join(base.fsPath, ...parts)) },
    workspace: { isTrusted: true, workspaceFolders: [], textDocuments: [], openTextDocument: async uri => ({ uri, lineCount: 100, lineAt: () => ({ text: 'code' }) }), getConfiguration: () => ({ get: (_, fallback) => fallback, inspect: () => ({ globalValue: 'configured' }) }), registerTextDocumentContentProvider: (_, p) => { provider = p; return disposable(); } },
    window: { showOpenDialog: async () => [uri(path.join(storage, 'runs', '20261014T000000Z-abcdef123456'))], showTextDocument: async (document, options) => { commands.push(['showText', document.uri.fsPath, options]); }, createOutputChannel: () => ({ clear() {}, append() {}, appendLine() {}, show() {}, dispose() {} }), registerWebviewViewProvider: (id, registered, options) => {
      assert.equal(id, 'miniAgent.sidebar');
      assert.equal(options.webviewOptions.retainContextWhenHidden, true);
      viewProvider = registered; return disposable();
    }, createTestView: () => {
      panel = { show() {}, webview: { cspSource: 'resource:', asWebviewUri: u => u.toString(), postMessage: m => { messages.push(m); return Promise.resolve(true); }, onDidReceiveMessage: fn => { receive = fn; return disposable(); } }, onDidDispose: fn => { disposePanel = () => { fn(); panel = null; }; return disposable(); } };
      return panel;
    } },
    commands: { registerCommand: (_, fn) => { open = fn; return disposable(); }, executeCommand: async (...args) => { commands.push(args); if (args[0] === 'miniAgent.sidebar.focus' && !panel) viewProvider.resolveWebviewView(vscode.window.createTestView()); } },
    ViewColumn: { One: 1 }, Range: class { constructor(...coordinates) { this.coordinates = coordinates; } }
  };
  const originalLoad = Module._load;
  try {
    Module._load = function(name, ...args) { return name === 'vscode' ? vscode : name === './agent-process' ? { AgentProcess: FakeProcess } : originalLoad.call(this, name, ...args); };
    const { activate } = require('../src/extension');
    Module._load = originalLoad;
    activate(context);
    await open();
    assert.match(panel.webview.html, /default-src 'none'/);
    assert.match(panel.webview.html, /nonce-/);
    assert.equal(vscode.window.createWebviewPanel, undefined);
    vscode.workspace.workspaceFolders = [{ uri: uri(storage) }];
    await receive({ type: 'ready' });
    assert.equal(messages.find(m => m.type === 'init').project, storage);
    const project = path.resolve(__dirname, '../../../examples/buggy_project');
    const request = { type: 'start', project, task: "text $(do-not-execute) 'quote'", model: 'mock', mode: 'readonly' };
    await receive(request);
    assert.equal(launched.python, 'python');
    assert.equal(launched.args[2], 'mini_agent.panel');
    assert.equal(launched.args[launched.args.indexOf('--task') + 1], request.task);
    assert.equal(launched.options.cwd, project);
    assert.ok(messages.some(m => m.type === 'state' && m.active));
    await receive(request);
    assert.match(messages.at(-1).message, /活动任务/);
    const id = '20261014T000000Z-abcdef123456';
    await fs.mkdir(path.join(storage, 'runs', id), { recursive: true });
    launched.emit('message', { type: 'run_ready', run_id: id });
    assert.deepEqual(launched.sent.at(-1), { type: 'editor_state', dirty_paths: [] });
    launched.emit('message', { type: 'proposal', payload: { files: [{ path: 'src/calculator.py', original: 'old', replacement: 'new' }] } });
    await receive({ type: 'preview', path: 'src/calculator.py' });
    const diff = commands.find(args => args[0] === 'vscode.diff');
    assert.equal(provider.provideTextDocumentContent(diff[1]), 'old');
    assert.equal(provider.provideTextDocumentContent(diff[2]), 'new');
    launched.emit('message', { type: 'approval_requested', approval_id: 'approval', kind: 'proposal', payload: { diff: 'diff', files: [{ path: 'src/calculator.py', original: 'old', replacement: 'new' }] } });
    await new Promise(resolve => setTimeout(resolve, 20));
    disposePanel();
    assert.equal(launched.cancelled, undefined, 'closing the panel must not cancel the agent');
    await open();
    await receive({ type: 'ready' });
    await new Promise(resolve => setTimeout(resolve, 20));
    const waiting = messages.filter(m => m.type === 'state' && m.pending).at(-1);
    assert.equal(waiting.pending.id, 'approval');
    assert.deepEqual(waiting.pending.payload.files, ['src/calculator.py']);
    assert.equal(waiting.pending.payload.original, undefined);
    await receive({ type: 'approval', id: 'stale', approved: true });
    assert.equal(launched.decision, undefined);
    vscode.workspace.textDocuments.push({ isDirty: true, uri: uri(path.join(project, 'src/calculator.py')) });
    await receive({ type: 'approval', id: 'approval', approved: true });
    assert.equal(launched.decision, undefined);
    vscode.workspace.textDocuments.length = 0;
    await receive({ type: 'approval', id: 'approval', approved: true });
    assert.deepEqual(launched.decision, { id: 'approval', approved: true });
    assert.deepEqual(launched.sent.at(-1), { type: 'editor_state', dirty_paths: [] });
    await receive({ type: 'cancel' });
    assert.equal(launched.cancelled, true);
    launched.emit('exit', 1);
    assert.ok(messages.some(m => m.type === 'exit' && m.code === 1));
    assert.equal(saved.get('miniAgent.selectedRun'), id);
    await fs.writeFile(path.join(storage, 'runs', id, 'manifest.json'), JSON.stringify({ version: 1, project, transactions: [{ status: 'applied', files: [{ path: 'src/calculator.py', state: 'written' }] }] }));
    await receive({ type: 'restore', id });
    assert.equal(launched.args[3], 'rollback');
    assert.equal(launched.args.at(-1), id);
    launched.emit('message', { type: 'approval_requested', approval_id: 'restore-id', kind: 'rollback', payload: { project, files: ['src/calculator.py'] } });
    await receive({ type: 'approval', id: 'restore-id', approved: true });
    assert.deepEqual(launched.decision, { id: 'restore-id', approved: true });
    launched.emit('message', { type: 'recovery_result', status: 'rolled_back', files: ['src/calculator.py'] });
    assert.ok(messages.some(m => m.type === 'recovery_result' && m.status === 'rolled_back'));
    launched.emit('exit', 0);
    disposePanel();
    // A new extension activation reloads the saved selection, not only Webview memory.
    activate(context);
    await open();
    await receive({ type: 'ready' });
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(messages.filter(m => m.type === 'state').at(-1).state.id, id);
    await receive({ type: 'restore', id });
    launched.emit('exit', 2);
    assert.ok(messages.some(m => m.type === 'error' && m.message.includes('恢复进程未产生完整结果')));
    const digest = crypto.createHash('sha256').update(await fs.readFile(path.join(project, 'src/calculator.py'))).digest('hex');
    await fs.writeFile(path.join(storage, 'runs', id, 'events.jsonl'), JSON.stringify({ event: 'started', project, task: 'debug' }) + '\n');
    await fs.writeFile(path.join(storage, 'runs', id, 'diagnosis.json'), JSON.stringify({ evidence: [{ id: 'E1', kind: 'code', path: 'src/calculator.py', line: 1, sha256: digest }] }));
    await receive({ type: 'source', id, ref: 'evidence:E1' });
    assert.ok(commands.some(command => command[0] === 'showText' && command[1].endsWith(path.join('src', 'calculator.py'))));
    await receive({ type: 'source', id, ref: '__proto__' });
    assert.ok(messages.some(m => m.type === 'error' && m.message.includes('不属于当前运行')));
    await receive({ type: 'import-run' });
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(messages.filter(m => m.type === 'state').at(-1).imported, true);
    const previous = launched;
    await receive({ type: 'restore', id });
    assert.equal(launched, previous, 'imported records must remain read-only');
    await receive({ type: 'local-history' });
    const profile = { endpoint: 'https://example.invalid/v1/chat/completions', model: 'test-model', protocol: 'chat_completions', max_run_cost_cny: 0.5 };
    await receive({ type: 'save-api', profile, key: 'test-secret-value' });
    assert.equal(messages.at(-1).panelManaged, true);
    assert.equal(messages.at(-1).hasKey, true);
    assert.ok(!JSON.stringify(messages).includes('test-secret-value'));
    await receive({ ...request, model: 'api' });
    assert.equal(launched.options.env.MINI_AGENT_PANEL_API_KEY, 'test-secret-value');
    const configPath = launched.args[launched.args.indexOf('--config') + 1];
    const apiConfig = await fs.readFile(configPath, 'utf8');
    assert.equal(JSON.parse(apiConfig).api.model, 'test-model');
    assert.ok(!apiConfig.includes('test-secret-value'));
    await receive({ type: 'clear-api' });
    assert.equal(secrets.size, 1, 'active runs block configuration changes');
    launched.emit('exit', 0);
    await receive({ type: 'clear-api' });
    assert.equal(secrets.size, 0);
    assert.equal(messages.at(-1).panelManaged, false);
    disposePanel();
  } finally {
    Module._load = originalLoad;
    if (disposePanel) disposePanel();
    // Let the final refresh settle before removing its storage directory.
    await new Promise(resolve => setTimeout(resolve, 30));
    await fs.rm(storage, { recursive: true, force: true });
  }
});
