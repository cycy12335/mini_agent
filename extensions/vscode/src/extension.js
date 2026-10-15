'use strict';
const vscode = require('vscode');
const path = require('node:path');
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const { buildArgs, listRuns, snapshot, RUN_ID } = require('./runtime');
const { AgentProcess } = require('./agent-process');
const { checkedSource } = require('./insights');
const { settingsFor } = require('./settings');
const { ApiSettings } = require('./api-settings');

function activate(context) {
  const apiSettings = new ApiSettings(context);
  let panel, execution, selectedRun, pollBusy = false, disposed = false;
  let launch = null, starting = false, configuring = false, pending = null, proposal = null;
  selectedRun = context.globalState?.get('miniAgent.selectedRun');
  let imported = context.globalState?.get('miniAgent.importedRun') || null;
  if (imported && (typeof imported.root !== 'string' || !path.isAbsolute(imported.root) || !RUN_ID.test(imported.id))) imported = null;
  const virtualDocuments = new Map();
  const output = vscode.window.createOutputChannel('Mini Agent');
  context.subscriptions.push(output);
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider('mini-agent-preview', {
    provideTextDocumentContent: uri => virtualDocuments.get(uri.toString()) || ''
  }));
  context.subscriptions.push({ dispose: () => execution?.dispose() });
  const runsRoot = path.join(context.globalStorageUri.fsPath, 'runs');
  function syncEditorState() {
    if (!execution?.send || !launch?.project) return;
    const dirty_paths = vscode.workspace.textDocuments.filter(doc => doc.isDirty && doc.uri.scheme === 'file' && inside(launch.project, doc.uri.fsPath))
      .map(doc => path.relative(launch.project, doc.uri.fsPath).split(path.sep).join('/'));
    try { execution.send({ type: 'editor_state', dirty_paths }); } catch { /* Process may have exited. */ }
  }
  for (const event of ['onDidChangeTextDocument', 'onDidSaveTextDocument', 'onDidCloseTextDocument']) {
    if (vscode.workspace[event]) context.subscriptions.push(vscode.workspace[event](syncEditorState));
  }
  const localDisposables = [];
  async function refresh() {
    if (!panel || pollBusy) return;
    pollBusy = true;
    try {
      const runs = imported ? [imported.id] : await listRuns(runsRoot);
      if (selectedRun && !runs.includes(selectedRun)) selectedRun = undefined;
      if (!selectedRun && (!launch || imported)) selectedRun = runs[0];
      const state = selectedRun ? await snapshot(imported?.root || runsRoot, selectedRun) : null;
      panel?.webview.postMessage({ type: 'state', runs, state, active: Boolean(execution), launching: starting,
        imported: Boolean(imported),
        connected: Boolean(execution && selectedRun === launch?.runId), operation: launch?.operation,
        pending: pending ? { id: pending.approval_id, kind: pending.kind, runId: pending.run_id,
          payload: pending.kind === 'proposal' ? { diff: pending.payload.diff, reason: pending.payload.reason,
            notice: pending.payload.notice, files: pending.payload.files.map(file => file.path) } : pending.payload } : null,
        previewFiles: !imported && selectedRun === launch?.runId && launch?.operation === 'run' ? (proposal?.files || []).map(file => file.path) : [] });
    } catch (error) { panel?.webview.postMessage({ type: 'error', message: error.message }); }
    finally { pollBusy = false; }
  }

  async function start(input) {
    if (configuring) throw new Error('API 配置正在保存，请稍后启动任务。');
    if (starting || execution) throw new Error('已有活动任务，请先完成或取消它。');
    starting = true;
    try {
    if (!vscode.workspace.isTrusted) throw new Error('请先信任工作区。');
    let settings = await settingsFor(vscode.workspace.getConfiguration('miniAgent'), context.extensionUri.fsPath);
    let prepared;
    if (input.model === 'api') {
      prepared = await apiSettings.prepare(settings, context.globalStorageUri.fsPath);
      settings = prepared.settings;
    }
    try {
    const args = buildArgs(input, settings, runsRoot);
    if (!(await fs.stat(input.project)).isDirectory()) throw new Error('仓库路径必须是目录。');
    // Avoid writing over unsaved editor content during approval-required runs.
    if (input.mode !== 'readonly') {
      const dirty = vscode.workspace.textDocuments.some(doc => doc.isDirty && doc.uri.scheme === 'file' && inside(input.project, doc.uri.fsPath));
      if (dirty) throw new Error('目标仓库中有未保存文件，请先保存后启动修改任务。');
    }
    const cwd = settings.configPath ? path.dirname(settings.configPath) : input.project;
    // Keep launch arguments literal; the panel module owns the versioned stdio adapter.
    args.splice(2, 2, 'mini_agent.panel');
    launch = { project: path.resolve(input.project), task: input.task.trim(), operation: 'run', ended: false };
    imported = null;
    void context.globalState?.update('miniAgent.importedRun', undefined);
    pending = proposal = null;
    virtualDocuments.clear();
    selectedRun = null;
    output.clear();
    connect(settings.pythonPath, args, cwd, prepared?.env, prepared?.cleanup);
    prepared = undefined;
    await refresh();
    } finally { await prepared?.cleanup?.(); }
    } finally { starting = false; }
  }

  function connect(python, args, cwd, extraEnv = {}, cleanup) {
    const agentProcess = new AgentProcess(python, args, { cwd, env: { ...process.env, ...extraEnv, PYTHONIOENCODING: 'utf-8' } });
    execution = agentProcess;
    agentProcess.on('message', event => {
      if (event.type === 'run_ready' && RUN_ID.test(event.run_id)) {
        launch.runId = event.run_id; selectedRun = event.run_id;
        syncEditorState();
        void context.globalState?.update('miniAgent.selectedRun', selectedRun);
      }
      if (event.type === 'approval_requested') pending = event;
      if (event.type === 'approval_resolved' && pending?.approval_id === event.approval_id) pending = null;
      if (event.type === 'proposal') proposal = event.payload;
      if (event.type === 'display') output.appendLine(event.text);
      if (event.type === 'command_error') panel?.webview.postMessage({ type: 'error', message: event.message });
      if (event.type === 'recovery_result') { launch.recoveryResult = event; panel?.webview.postMessage(event); }
      void refresh();
    });
    agentProcess.on('diagnostic', text => output.append(text));
    agentProcess.on('fault', message => { output.appendLine(message); panel?.webview.postMessage({ type: 'error', message }); });
    agentProcess.on('exit', (code, signal) => {
      if (cleanup) void cleanup().catch(() => output.appendLine('无法清理临时 API 配置目录。'));
      if (execution === agentProcess) { execution = null; pending = null; launch.ended = true; }
      if (launch?.operation === 'rollback' && !launch.recoveryResult) {
        panel?.webview.postMessage({ type: 'error', message: `恢复进程未产生完整结果（退出码 ${code ?? '未知'}）。请检查恢复清单和运行输出，并确认使用 0.3.0 或更新的 Agent。` });
      }
      panel?.webview.postMessage({ type: 'exit', code, signal, operation: launch?.operation, hasRun: Boolean(launch?.runId) });
      void refresh();
    });
  }

  async function restore(id) {
    if (starting || execution) throw new Error('请先完成或取消活动任务，再预览恢复。');
    if (imported) throw new Error('导入的运行记录仅供查看，不能在这里恢复。');
    starting = true;
    try {
      if (!vscode.workspace.isTrusted) throw new Error('请先信任工作区。');
      if (id !== selectedRun || !(await listRuns(runsRoot)).includes(id)) throw new Error('历史选择已变化，请重新预览恢复。');
      const state = await snapshot(runsRoot, id);
      if (!state.manifest || !['applied', 'pending', 'conflict'].includes(state.recovery.status)) throw new Error('本次运行没有待恢复的修改。');
      if (typeof state.manifest.project !== 'string' || !path.isAbsolute(state.manifest.project)) throw new Error('恢复清单中的项目路径无效。');
      const project = state.manifest.project;
      if (vscode.workspace.textDocuments.some(doc => doc.isDirty && doc.uri.scheme === 'file' && inside(project, doc.uri.fsPath))) throw new Error('目标仓库有未保存内容，请先保存再预览恢复。');
      launch = { project, runId: id, operation: 'rollback', ended: false };
      pending = proposal = null;
      output.clear();
      const settings = await settingsFor(vscode.workspace.getConfiguration('miniAgent'), context.extensionUri.fsPath);
      connect(settings.pythonPath,
        ['-u', '-m', 'mini_agent.panel', 'rollback', '--runs-dir', runsRoot, '--run-id', id], path.dirname(runsRoot));
      await refresh();
    } finally { starting = false; }
  }

  function inside(root, file) {
    const relative = path.relative(path.resolve(root), path.resolve(file));
    return !relative || relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative);
  }

  async function onMessage(message) {
    if (!message || typeof message.type !== 'string') return;
    try {
      if (message.type === 'ready') {
        const folder = currentFolder();
        panel.webview.postMessage({ type: 'init', project: folder?.uri.scheme === 'file' ? folder.uri.fsPath : '' });
        await sendApiSettings();
        await refresh();
      } else if (message.type === 'browse') {
        const picks = await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, canSelectMany: false });
        if (picks?.[0]?.scheme === 'file') panel?.webview.postMessage({ type: 'project', project: picks[0].fsPath });
      } else if (message.type === 'start') await start(message);
      else if (message.type === 'import-run') {
        if (execution || starting) throw new Error('请先完成或取消活动任务，再导入记录。');
        const picks = await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, canSelectMany: false, title: '选择含 events.jsonl 的运行编号文件夹' });
        if (!picks?.[0] || picks[0].scheme !== 'file') return;
        const directory = picks[0].fsPath;
        const id = path.basename(directory);
        const root = path.dirname(directory);
        const state = await snapshot(root, id);
        if (!state.started && !state.summary) throw new Error('该目录不是有效的 Agent 运行记录。');
        if (execution || starting) throw new Error('已有活动任务，暂不能切换导入记录。');
        imported = { root, id }; selectedRun = id;
        void context.globalState?.update('miniAgent.importedRun', imported);
        void context.globalState?.update('miniAgent.selectedRun', id);
        await refresh();
      } else if (message.type === 'local-history') {
        imported = null; selectedRun = launch?.runId;
        void context.globalState?.update('miniAgent.importedRun', undefined);
        await refresh();
      } else if (message.type === 'source') {
        if (message.id !== selectedRun) throw new Error('历史选择已变化，请重新点击代码位置。');
        const state = await snapshot(imported?.root || runsRoot, selectedRun);
        if (!Object.hasOwn(state.insights.locations, message.ref)) throw new Error('代码引用不属于当前运行。');
        const location = state.insights.locations[message.ref];
        const file = await checkedSource(state.started?.project || state.manifest?.project, location);
        if (vscode.workspace.textDocuments.some(doc => doc.isDirty && doc.uri.scheme === 'file' && path.resolve(doc.uri.fsPath) === path.resolve(file))) throw new Error('文件有未保存的编辑，历史行号可能过期。');
        const document = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
        const end = Math.max(location.line, Math.min(document.lineCount, Number.isInteger(location.end_line) ? location.end_line : location.line));
        await vscode.window.showTextDocument(document, { preview: true, selection: new vscode.Range(location.line - 1, 0, end - 1, document.lineAt(end - 1).text.length) });
      }
      else if (message.type === 'restore') await restore(message.id);
      else if (message.type === 'cancel') { execution?.cancel(); }
      else if (message.type === 'approval') {
        if (!execution || !pending || message.id !== pending.approval_id || typeof message.approved !== 'boolean') throw new Error('审批已失效。');
          if (message.approved && ['proposal', 'rollback', 'test_expectation'].includes(pending.kind)) {
          const dirty = vscode.workspace.textDocuments.some(doc => doc.isDirty && doc.uri.scheme === 'file' && inside(launch.project, doc.uri.fsPath));
          if (dirty) throw new Error('仓库中有未保存的修改，请先保存，再检查 Diff。');
        }
          syncEditorState();
          execution.approve(message.id, message.approved);
        pending = null;
        await refresh();
      } else if (message.type === 'preview') {
        if (selectedRun !== launch?.runId || !proposal) throw new Error('当前运行没有可用的原生 Diff。历史运行请查看补丁文件。');
        const edit = proposal.files.find(file => file.path === message.path);
        if (!edit) throw new Error('无效的修改文件。');
        const key = crypto.randomBytes(8).toString('hex');
        const left = vscode.Uri.parse(`mini-agent-preview:/${key}/original/${encodeURIComponent(edit.path)}`);
        const right = vscode.Uri.parse(`mini-agent-preview:/${key}/proposed/${encodeURIComponent(edit.path)}`);
        virtualDocuments.set(left.toString(), edit.original);
        virtualDocuments.set(right.toString(), edit.replacement);
        await vscode.commands.executeCommand('vscode.diff', left, right, `${edit.path} · 原始 / 提案`, { preview: true });
      }
      else if (message.type === 'select') {
        if (typeof message.id !== 'string' || !RUN_ID.test(message.id) || !(imported ? [imported.id] : await listRuns(runsRoot)).includes(message.id)) throw new Error('请选择已有运行记录。');
        selectedRun = message.id;
        void context.globalState?.update('miniAgent.selectedRun', selectedRun);
        await refresh();
      } else if (message.type === 'artifact') {
        if (message.id && message.id !== selectedRun) throw new Error('历史选择已变化，请重新点击执行记录。');
        if (!selectedRun || (!['proposal.diff', 'report.md', 'test-output.txt', 'diagnosis.json'].includes(message.name) && !/^test-\d{3,}(?:\.json|-results\.json|-output\.txt)$/.test(message.name || ''))) throw new Error('无效的产物。');
        const file = path.join(imported?.root || runsRoot, selectedRun, message.name);
        if ((await fs.lstat(file)).isSymbolicLink()) throw new Error('不打开链接产物。');
        await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(vscode.Uri.file(file)), { preview: true });
      } else if (message.type === 'terminal') {
        output.show(true);
      } else if (message.type === 'settings') {
        await sendApiSettings();
      } else if (message.type === 'save-api') {
        await changeApi(() => apiSettings.save(message.profile, message.key));
      } else if (message.type === 'clear-api') {
        await changeApi(() => apiSettings.clear());
      } else if (message.type === 'advanced-settings') {
        await vscode.commands.executeCommand('workbench.action.openSettings', 'miniAgent');
      } else if (message.type === 'refresh') await refresh();
    } catch (error) { panel?.webview.postMessage({ type: 'error', message: error.message }); }
  }

  async function changeApi(action) {
    if (execution || starting) throw new Error('请先完成或取消当前任务，再修改 API 设置。');
    if (configuring) throw new Error('正在保存 API 配置，请稍候。');
    if (!vscode.workspace.isTrusted) throw new Error('请先信任工作区。');
    configuring = true;
    try { await action(); await sendApiSettings(true); }
    finally { configuring = false; }
  }

  async function sendApiSettings(saved = false) {
    const settings = await settingsFor(vscode.workspace.getConfiguration('miniAgent'), context.extensionUri.fsPath);
    panel?.webview.postMessage({ ...await apiSettings.view(settings.configPath), saved });
  }

  function currentFolder() {
    const active = vscode.window.activeTextEditor?.document.uri;
    return (active && vscode.workspace.getWorkspaceFolder?.(active)) || vscode.workspace.workspaceFolders?.[0];
  }
  if (vscode.workspace.onDidChangeWorkspaceFolders) context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => {
    const folder = currentFolder();
    panel?.webview.postMessage({ type: 'workspace', project: folder?.uri.scheme === 'file' ? folder.uri.fsPath : '' });
  }));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider('miniAgent.sidebar', {
    resolveWebviewView(view) {
      panel = view;
      disposed = false;
      const media = vscode.Uri.joinPath(context.extensionUri, 'media');
      view.webview.options = { enableScripts: true, localResourceRoots: [media] };
      const nonce = crypto.randomBytes(16).toString('hex');
      const script = view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'panel.js'));
      const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'panel.css'));
      const insights = view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'insights.js'));
      view.webview.html = html(view.webview.cspSource, nonce, script, css, insights);
      localDisposables.push(view.webview.onDidReceiveMessage(onMessage));
      const timer = setInterval(() => { if (!disposed) void refresh(); }, 1000);
      const cleanup = () => {
        clearInterval(timer);
        if (panel !== view) return;
        disposed = true; panel = null;
        for (const disposable of localDisposables.splice(0)) disposable.dispose();
      };
      context.subscriptions.push({ dispose: cleanup });
      view.onDidDispose(cleanup, null, context.subscriptions);
    }
  }, { webviewOptions: { retainContextWhenHidden: true } }));
  context.subscriptions.push(vscode.commands.registerCommand('miniAgent.open', async () => {
    await vscode.commands.executeCommand('miniAgent.sidebar.focus');
    panel?.show?.(true);
  }));
}

function html(source, nonce, script, css, insights) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${source}; script-src 'nonce-${nonce}';">
  <link rel="stylesheet" href="${css}"><title>Mini Agent</title></head><body>
  <header><div><h1>Mini Agent</h1><p>描述问题，让 Agent 定位与修复。</p></div><button id="settings" class="secondary">设置</button></header>
  <main><details id="api-settings" class="card"><summary>模型与 API 配置</summary>
  <form id="api-form">
  <label for="api-endpoint">API 完整请求地址</label><input id="api-endpoint" type="url" required placeholder="https://服务商地址/v1/chat/completions">
  <label for="api-protocol">API 协议</label><select id="api-protocol"><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select>
  <label for="api-model">模型 ID（选择曾用模型或自行填写）</label><input id="api-model" list="api-models" required maxlength="200" placeholder="填写服务商提供的模型 ID"><datalist id="api-models"></datalist>
  <label for="api-key">API Key</label><input id="api-key" type="password" autocomplete="off" spellcheck="false" placeholder="首次输入；留空保留已保存的 Key">
  <p id="api-key-status" class="hint"></p>
  <details><summary>预算与推理选项</summary>
  <label for="api-budget">单次预算上限（元，最多 0.5）</label><input id="api-budget" type="number" min="0.001" max="0.5" step="any" value="0.5" required>
  <label for="api-input-price">输入单价（元 / 百万 token，可留空）</label><input id="api-input-price" type="number" min="0" step="any">
  <label for="api-output-price">输出单价（元 / 百万 token，可留空）</label><input id="api-output-price" type="number" min="0" step="any">
  <label for="api-reasoning">推理强度（须由模型支持）</label><select id="api-reasoning"><option value="">不指定</option><option>none</option><option>low</option><option>medium</option><option>high</option><option>max</option></select>
  <p class="hint">更换模型时请核对两项单价。未知单价留空时，核心使用现有保守预算策略；不会自动查询服务商。</p></details>
  <p class="hint">Key 保存在 VS Code 安全存储中，不回显、不写入项目文件。配置适用于本机此 VS Code 用户的所有项目。</p>
  <div class="row actions"><button id="api-save" type="submit">保存配置</button><button id="clear-api" type="button" class="secondary">清除面板配置与 Key</button><button id="advanced-settings" type="button" class="secondary">解释器设置</button></div>
  <p id="api-notice" role="status" aria-live="polite"></p></form></details>
  <section class="card"><h2>新任务</h2><form id="task-form">
  <label for="project">当前仓库（自动使用工作区）</label><div class="row"><input id="project" required placeholder="打开项目或选择目录"><button type="button" id="browse" class="secondary">更换</button></div>
  <label for="task">自然语言任务 / Bug 描述</label><textarea id="task" required maxlength="4096" rows="5" placeholder="例如：空列表导致平均值函数崩溃，请定位原因并修复"></textarea>
  <details><summary>运行选项 · 模型与权限</summary><div class="row options"><div><label for="model">模型</label><select id="model"><option value="api">已配置的 API 模型</option><option value="mock">Mock 示例演示</option></select></div>
  <div><label for="mode">执行方式</label><select id="mode"><option value="readonly">只读分析</option><option value="approve">确认后修改，不运行测试</option><option value="tests">确认后修改，并授权运行测试</option></select></div></div>
  <p class="hint">默认只读分析；需要修复请选择“确认后修改”。发送与修改均需确认。Mock 仅支持 average 示例；测试会执行项目代码。</p></details>
  <button id="start" type="submit">开始任务</button></form><p id="notice" role="status" aria-live="polite"></p></section>
  <section class="card"><div class="section-head"><h2>运行记录</h2><button id="refresh" class="secondary">刷新</button></div>
  <div class="row actions"><button id="import-run" class="secondary">导入运行记录</button><button id="local-history" class="secondary">返回本地记录</button></div><p id="import-note" class="hint"></p>
  <label for="runs">最近运行</label><select id="runs"><option value="">暂无记录</option></select>
  <div class="status-row"><strong id="status">等待任务</strong><span id="run-id"></span></div>
  <p id="history-project" class="hint"></p>
  <p id="conclusion">提交任务后，这里会显示核心记录的执行事件。</p>
  <div class="row actions"><button id="cancel" class="secondary" disabled>取消任务</button><button id="terminal" class="secondary">查看运行输出</button><button id="diff" class="secondary" disabled>查看补丁</button><button id="report" class="secondary" disabled>查看报告</button></div>
  <section id="approval" class="verification" hidden aria-live="polite"><h3 id="approval-title"></h3><pre id="approval-detail"></pre><div class="row"><button id="approve">同意并继续</button><button id="decline" class="secondary">拒绝</button></div></section>
  <div id="preview-files" class="row actions"></div>
  <div id="verification" class="verification" hidden></div>
  <details id="test-details" hidden><summary>查看具体测试用例</summary><pre id="test-cases"></pre></details>
  <section class="verification"><h3>恢复清单状态</h3><p id="recovery-state">尚无修改记录</p><p id="recovery-note" class="hint"></p><button id="restore" class="secondary" disabled>预览恢复</button></section>
  <section class="insight-section"><h3>定位与证据</h3><p id="diagnosis-status"></p><p id="root-cause"></p><p class="hint">以下是模型记录的假设和可追溯证据，不代表语义因果已经独立证明。</p><div id="hypotheses"></div><details><summary>查看证据与未确认项</summary><div id="evidence"></div><pre id="unresolved"></pre></details></section>
  <section class="insight-section"><h3>相关调用关系</h3><label for="graph-focus">聚焦符号</label><select id="graph-focus"><option value="">全部相关关系</option></select><p id="graph-note" class="hint"></p><div id="call-graph"></div><div id="call-sites"></div></section>
  <section class="insight-section"><h3>运行预算</h3><p id="budget"></p><p class="hint">费用是客户端估计，包含未确定用量的预留；不是服务商账单。</p></section>
  <details><summary>执行时间线（最近 100 条事件）</summary><ol id="timeline"><li class="empty">尚无执行事件</li></ol></details>
  </section></main><footer>Agent 独立运行 · 面板审批与修复验证</footer>
  <script nonce="${nonce}" src="${insights}"></script><script nonce="${nonce}" src="${script}"></script></body></html>`;
}

module.exports = { activate };
