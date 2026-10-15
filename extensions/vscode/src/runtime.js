'use strict';
const path = require('node:path');
const fs = require('node:fs/promises');
const { buildInsights } = require('./insights');
const RUN_ID = /^\d{8}T\d{6}Z-[0-9a-f]{12}$/;

function buildArgs(input, settings, runsRoot) {
  if (typeof input.project !== 'string' || !path.isAbsolute(input.project) || /[\r\n\0]/.test(input.project)) throw new Error('仓库路径必须是绝对路径。');
  if (typeof input.task !== 'string' || !input.task.trim() || input.task.length > 4096 || input.task.includes('\0')) throw new Error('Bug 描述需要 1–4096 个字符。');
  if (!['mock', 'api'].includes(input.model) || !['readonly', 'approve', 'tests'].includes(input.mode)) throw new Error('无效的运行模式。');
  if (input.model === 'api' && !settings.configPath) throw new Error('请先配置 miniAgent.configPath。');
  if (settings.configPath && !path.isAbsolute(settings.configPath)) throw new Error('配置文件必须使用绝对路径。');
  const project = path.resolve(input.project);
  const relative = path.relative(project, path.resolve(runsRoot));
  if (!relative || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative))) throw new Error('运行记录目录必须位于目标仓库之外。');
  const args = ['-u', '-m', 'mini_agent', 'run', '--project', project, '--task', input.task.trim(), '--model', input.model, '--runs-dir', runsRoot];
  if (settings.configPath) args.push('--config', settings.configPath);
  if (input.mode === 'readonly') args.push('--read-only');
  if (input.mode === 'tests') {
    if (!settings.testPythonPath || !path.isAbsolute(settings.testPythonPath)) throw new Error('请配置测试解释器绝对路径 miniAgent.testPythonPath。');
    args.push('--allow-tests', '--test-python', settings.testPythonPath);
  }
  return args;
}

function parseEvents(text) {
  // The writer may still be appending its final line. Never publish partial JSON.
  return text.split('\n').slice(0, -1).filter(Boolean).map(line => JSON.parse(line));
}

async function readBounded(file, limit = 8 * 1024 * 1024) {
  const handle = await fs.open(file, 'r');
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > limit) throw new Error('运行记录超出读取限制。');
    const bytes = Buffer.alloc(Math.min(limit + 1, info.size + 1));
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead > limit) throw new Error('运行记录超出读取限制。');
    return bytes.subarray(0, bytesRead).toString('utf8');
  } finally { await handle.close(); }
}

async function listRuns(root) {
  try {
    const entries = await fs.readdir(root, { withFileTypes: true });
    return entries.filter(e => e.isDirectory() && RUN_ID.test(e.name)).map(e => e.name).sort().reverse().slice(0, 50);
  } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
}

async function snapshot(root, id) {
  if (!RUN_ID.test(id)) throw new Error('无效的运行编号。');
  const directory = path.join(root, id);
  if ((await fs.lstat(directory)).isSymbolicLink()) throw new Error('不读取链接运行目录。');
  const optional = async name => {
    try {
      const file = path.join(directory, name);
      if ((await fs.lstat(file)).isSymbolicLink()) throw new Error('不读取链接产物。');
      return await readBounded(file);
    } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  };
  const [events, summary, diff, report, manifest, rollback, diagnosis] = await Promise.all(['events.jsonl', 'summary.json', 'proposal.diff', 'report.md', 'manifest.json', 'rollback.json', 'diagnosis.json'].map(optional));
  const parsed = events ? parseEvents(events) : [];
  const state = manifest ? JSON.parse(manifest) : null;
  const summaryState = summary ? JSON.parse(summary) : null;
  return { id, started: parsed.find(e => e.event === 'started') || null, events: parsed.slice(-100), summary: summaryState,
    insights: buildInsights(parsed, diagnosis ? JSON.parse(diagnosis) : null, summaryState),
    manifest: state, rollback: rollback ? JSON.parse(rollback) : null, recovery: recoveryState(state),
    lastRecovery: [...parsed].reverse().find(event => ['rollback_failed', 'rollback_cancelled', 'rollback_finished', 'rollback_approval'].includes(event.event)) || null,
    hasDiff: diff !== null, hasReport: report !== null };
}

function recoveryState(manifest) {
  if (!manifest) return { status: 'unchanged', files: [] };
  if (manifest.version !== 1 || !Array.isArray(manifest.transactions)) return { status: 'invalid', files: [] };
  const transactions = manifest.transactions;
  if (transactions.some(tx => !tx || !Array.isArray(tx.files) || tx.files.some(file => !file || typeof file.path !== 'string'))) return { status: 'invalid', files: [] };
  const files = [...new Set(transactions.filter(tx => !['rolled_back', 'recovered'].includes(tx.status))
    .flatMap(tx => tx.files.filter(file => ['writing', 'written'].includes(file.state)).map(file => file.path)))];
  if (transactions.some(tx => ['recovery_conflict', 'rollback_failed'].includes(tx.status))) return { status: 'conflict', files };
  if (transactions.some(tx => tx.status === 'prepared')) return { status: 'pending', files };
  if (files.length) return { status: 'applied', files };
  if (transactions.length && transactions.every(tx => ['rolled_back', 'recovered'].includes(tx.status))) return { status: 'restored', files: [] };
  return { status: 'unchanged', files: [] };
}

module.exports = { buildArgs, parseEvents, listRuns, snapshot, recoveryState, RUN_ID };
