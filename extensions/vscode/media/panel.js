'use strict';
const vscode = acquireVsCodeApi();
const $ = id => document.getElementById(id);
const saved = vscode.getState() || {};
let launching = false;
let approvalId = null;
let selectedId = null;
let apiDirty = false;
const apiFields = { endpoint: 'api-endpoint', protocol: 'api-protocol', model: 'api-model', max_run_cost_cny: 'api-budget',
  input_price_cny_per_million: 'api-input-price', output_price_cny_per_million: 'api-output-price', reasoning_effort: 'api-reasoning' };
for (const id of [...Object.values(apiFields), 'api-key']) $(id).addEventListener('input', () => { apiDirty = true; });
$('api-form').addEventListener('submit', event => {
  event.preventDefault();
  const key = $('api-key').value;
  $('api-key').value = '';
  $('api-save').disabled = true;
  $('api-notice').textContent = '正在保存…';
  send('save-api', { profile: Object.fromEntries(Object.entries(apiFields).map(([key, id]) => [key, $(id).value.trim()])), key });
});
$('settings').addEventListener('click', () => { $('api-settings').open = !$('api-settings').open; });
$('clear-api').addEventListener('click', () => { $('api-key').value = ''; send('clear-api'); });
for (const name of ['project', 'task', 'model', 'mode']) {
  if (typeof saved[name] === 'string') $(name).value = saved[name];
  $(name).addEventListener('change', persist);
}
function persist() {
  vscode.setState(Object.fromEntries(['project', 'task', 'model', 'mode'].map(name => [name, $(name).value])));
}
function send(type, fields = {}) { vscode.postMessage({ type, ...fields }); }
$('task-form').addEventListener('submit', event => {
  event.preventDefault();
  if ($('model').value === 'api' && apiDirty) {
    $('api-settings').open = true; $('notice').textContent = '请先保存模型与 API 配置，再开始任务。'; return;
  }
  persist(); launching = true; $('start').disabled = true;
  $('notice').textContent = '正在启动 Agent…';
  send('start', Object.fromEntries(['project', 'task', 'model', 'mode'].map(name => [name, $(name).value])));
});
for (const type of ['browse', 'refresh', 'terminal', 'import-run', 'local-history', 'advanced-settings']) $(type).addEventListener('click', () => send(type));
$('cancel').addEventListener('click', () => { send('cancel'); $('notice').textContent = '已请求取消，正在等待 Agent 收尾。'; });
$('restore').addEventListener('click', () => {
  if (!selectedId) return;
  $('restore').disabled = true;
  $('notice').textContent = '正在检查备份与文件冲突；预览不会恢复文件。';
  send('restore', { id: selectedId });
});
for (const [id, approved] of [['approve', true], ['decline', false]]) $(id).addEventListener('click', () => {
  if (!approvalId) return;
  $('approve').disabled = $('decline').disabled = true;
  send('approval', { id: approvalId, approved });
});
$('diff').addEventListener('click', () => send('artifact', { name: 'proposal.diff' }));
$('report').addEventListener('click', () => send('artifact', { name: 'report.md' }));
$('runs').addEventListener('change', () => { if ($('runs').value) send('select', { id: $('runs').value }); });
const labels = { started: '任务开始', disclosure_approval: '代码发送确认', action: '模型动作', tool_result: '工具结果', approval: '修改确认', patch_applied: '补丁已应用', tests: '测试结果', model_cost: '费用预算', finished: '任务结束', invalid_action: '无效动作', rollback_preview: '恢复预览', rollback_approval: '恢复确认', rollback_finished: '恢复完成', rollback_failed: '恢复未完成', rollback_cancelled: '恢复已取消' };
const statuses = { verified: '修复已验证', proposed: '分析 / 修改提案', unverified: '已修改，尚未验证', failed: '执行失败', cancelled: '已取消' };
function describe(e) {
  if (e.event === 'action') return e.action?.type === 'final' ? '提交最终结论' : e.action?.name || '动作';
  if (e.event === 'tool_result') return `${e.name}: ${e.result?.ok ? '成功' : '失败'} · ${e.result?.message || ''}`;
  if (e.event === 'tests') return `${e.phase} · 退出码 ${e.exit_code} · ${e.passed ? '通过' : '未通过'}`;
  if (['approval', 'disclosure_approval', 'rollback_approval'].includes(e.event)) return e.approved ? '已同意' : '已拒绝';
  if (e.event === 'rollback_failed') return `${e.error_code}: ${e.message}`;
  if (['rollback_preview', 'rollback_finished'].includes(e.event)) return (e.files || []).join(', ');
  if (e.event === 'patch_applied') return (e.files || []).join(', ');
  if (e.event === 'model_cost') return e.phase === 'reserved' ? '模型请求预算已预留' : '已收到用量记录';
  if (e.event === 'started') return e.task || '';
  if (e.event === 'finished') return `${statuses[e.status] || e.status} · ${e.reason}`;
  return e.error || e.status || '';
}
function render(message) {
  launching = message.launching;
  $('start').disabled = message.active || launching;
  $('import-run').disabled = message.active || launching;
  $('import-note').textContent = message.imported ? '正在查看导入记录（只读）。开始新任务会返回本地记录。' : '';
  $('cancel').disabled = !message.active;
  $('cancel').textContent = message.connected ? '取消任务' : '取消活动任务';
  const pending = message.pending;
  if (approvalId !== pending?.id) { $('approve').disabled = $('decline').disabled = false; }
  approvalId = pending?.id || null;
  $('approval').hidden = !pending;
  $('approval-title').textContent = pending?.kind === 'disclosure' ? '确认向模型服务发送代码' : pending?.kind === 'rollback' ? '确认恢复文件' : pending?.kind === 'test_expectation' ? '单独确认调整测试预期' : '确认应用修改';
  $('approve').textContent = pending?.kind === 'rollback' ? '确认恢复' : '同意并继续';
  $('approval-detail').textContent = pending?.kind === 'disclosure'
    ? `服务：${pending.payload.endpoint}\n模型：${pending.payload.model}\n协议：${pending.payload.protocol}\n预算上限：¥${pending.payload.budget_cny}\n${pending.payload.notice}\n允许按需读取：\n${pending.payload.files.join('\n')}`
    : pending?.kind === 'rollback' ? `运行：${pending.runId || ''}\n仓库：${pending.payload.project}\n${pending.payload.notice}\n待恢复文件：\n${pending.payload.files.join('\n')}`
    : [pending?.payload?.reason, pending?.payload?.notice, pending?.payload?.diff].filter(Boolean).join('\n');
  $('preview-files').replaceChildren(...(message.previewFiles || []).map(file => {
    const button = document.createElement('button'); button.className = 'secondary'; button.textContent = `Diff · ${file}`;
    button.addEventListener('click', () => send('preview', { path: file })); return button;
  }));
  $('runs').replaceChildren(...message.runs.map(id => {
    const option = document.createElement('option'); option.value = id; option.textContent = id; return option;
  }));
  if (!message.runs.length) { const option = document.createElement('option'); option.textContent = '暂无记录'; $('runs').append(option); }
  const state = message.state;
  selectedId = state?.id || null;
  if (state) $('runs').value = state.id;
  $('run-id').textContent = state?.id || '';
  $('history-project').textContent = state ? `仓库：${state.manifest?.project || state.started?.project || '未记录'}\n任务：${state.started?.task || '未记录'}` : '';
  $('diff').disabled = !state?.hasDiff;
  $('report').disabled = !state?.hasReport;
  $('status').textContent = message.connected ? pending ? '等待你的确认' : message.operation === 'rollback' ? '正在恢复' : '执行中' : state?.summary ? `历史结果：${statuses[state.summary.status] || state.summary.status}` : state ? '未完成记录 · 当前窗口未连接' : '等待任务';
  $('conclusion').textContent = message.connected ? pending ? '请检查发送范围、修改 Diff 或恢复清单，然后决定是否继续。' : 'Agent 正在执行，可请求取消。' : state?.summary?.message || (state ? '该运行没有完整结果，可能曾被中断。请检查恢复清单；不支持继续执行中断任务。' : '提交任务后，这里会显示执行事件。');
  const events = state?.events || [];
  $('timeline').replaceChildren(...events.map(e => {
    const item = document.createElement('li');
    const heading = document.createElement('strong'); heading.textContent = labels[e.event] || e.event;
    const time = document.createElement('time'); time.textContent = e.time ? new Date(e.time).toLocaleTimeString() : '';
    const detail = document.createElement('p'); detail.textContent = describe(e);
    item.append(heading, time, detail); return item;
  }));
  if (!events.length) { const item = document.createElement('li'); item.textContent = '尚无执行事件'; $('timeline').append(item); }
  const comparison = state?.summary?.comparison;
  $('verification').hidden = !state?.summary;
  $('verification').textContent = comparison ? `测试对比：已修复 ${(comparison.fixed_failures || []).length} · 剩余 ${(comparison.unresolved_failures || []).length} · 新增失败 ${(comparison.new_failures || []).length} · 缺失用例 ${(comparison.missing_cases || []).length}` : `测试状态：${state?.summary?.tests || '未运行'}。验证仅覆盖实际执行的测试。`;
  $('test-details').hidden = !comparison;
  $('test-cases').textContent = comparison ? [
    ['已修复', 'fixed_failures'], ['仍未解决', 'unresolved_failures'], ['新增失败', 'new_failures'],
    ['缺失用例', 'missing_cases'], ['新增跳过', 'newly_skipped'], ['原始收集错误', 'baseline_collection_errors'], ['当前收集错误', 'latest_collection_errors']
  ].map(([title, key]) => `${title}\n${(comparison[key] || []).join('\n') || '无'}`).join('\n\n') : '';
  const recovery = state?.recovery;
  const recoveryLabels = { unchanged: '本次运行没有已应用修改', applied: '修改已应用，可预览恢复', pending: '发现未完成写入清单，请预览检查', conflict: '恢复有冲突，请检查后重试', restored: '本次修改已恢复，恢复后未重新测试', invalid: '清单无效，请检查运行目录' };
  $('recovery-state').textContent = recovery ? `${recoveryLabels[recovery.status]}${recovery.files.length ? '\n' + recovery.files.join('\n') : ''}` : '尚无修改记录';
  $('recovery-note').textContent = state?.lastRecovery?.event === 'rollback_failed' ? `上次恢复未完成：${state.lastRecovery.error_code} · ${state.lastRecovery.message}` : '历史报告记录当时的验证结果。清单记录恢复操作状态；不代表后来手工编辑后的内容已验证。';
  $('restore').disabled = message.imported || message.active || launching || !['applied', 'pending', 'conflict'].includes(recovery?.status);
  window.MiniAgentInsights.render(state, send);
}
window.addEventListener('message', ({ data }) => {
  if (data.type === 'api-settings') {
    $('api-models').replaceChildren(...(data.models || []).map(model => {
      const option = document.createElement('option'); option.value = model; return option;
    }));
    for (const [key, id] of Object.entries(apiFields)) $(id).value = data.profile[key] ?? (key === 'max_run_cost_cny' ? '0.5' : key === 'protocol' ? 'chat_completions' : '');
    $('api-key').value = '';
    $('api-key-status').textContent = data.hasKey ? '已保存 Key。留空可保留；输入新 Key 可替换。' : '面板尚未保存 Key。未启用面板配置时沿用原配置文件与凭据。';
    $('api-notice').textContent = data.saved ? (data.panelManaged ? '已保存，下一次 API 任务使用此配置。' : '已清除面板配置，恢复使用原配置文件。') : (data.panelManaged ? '当前使用面板配置。' : '当前沿用原配置文件。填写 Key 并保存后启用面板配置。');
    $('api-save').disabled = false;
    apiDirty = false;
  }
  if (data.type === 'init' || data.type === 'workspace') { $('project').value = data.project || ''; persist(); }
  if (data.type === 'project') { $('project').value = data.project; persist(); }
  if (data.type === 'state') render(data);
  if (data.type === 'error') { launching = false; $('notice').textContent = data.message; $('api-notice').textContent = data.message; $('api-save').disabled = false; $('approve').disabled = $('decline').disabled = false; }
  if (data.type === 'exit') { launching = false; if (data.operation !== 'rollback') $('notice').textContent = data.hasRun ? `任务进程已结束（退出码 ${data.code ?? '未知'}）。以运行报告为准。` : 'Agent 未生成运行记录。请查看运行输出，检查 Python 路径和当前源码是否已安装。'; }
  if (data.type === 'recovery_result') {
    const labels = { rolled_back: '恢复完成。历史报告保持原结果，恢复后尚未重新测试。', cancelled: '已取消恢复，未开始恢复写入。', nothing_to_restore: '没有待恢复的修改。', failed: `恢复未完成：${data.error_code || ''} · ${data.message || '请检查清单和备份。'}` };
    $('notice').textContent = labels[data.status] || data.status;
  }
});
send('ready');
