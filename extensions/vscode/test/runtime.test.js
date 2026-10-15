'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const { buildArgs, parseEvents, listRuns, snapshot, recoveryState } = require('../src/runtime');
const project = path.resolve('fixture project');
const runsRoot = path.resolve('outside-runs');
const input = { project, task: "Bug 'quoted' $(command) `text`\nsecond line", model: 'mock', mode: 'readonly' };

test('task text is a single literal argument; readonly is the default UI mode', () => {
  const args = buildArgs(input, {}, runsRoot);
  assert.equal(args[args.indexOf('--task') + 1], input.task);
  assert.ok(args.includes('--read-only'));
  assert.ok(!args.includes('--allow-tests'));
});
test('API and test modes require explicit configuration', () => {
  assert.throws(() => buildArgs({ ...input, model: 'api' }, {}, runsRoot), /configPath/);
  assert.throws(() => buildArgs({ ...input, mode: 'tests' }, {}, runsRoot), /测试解释器/);
  const args = buildArgs({ ...input, mode: 'tests' }, { testPythonPath: path.resolve('python.exe') }, runsRoot);
  assert.ok(args.includes('--allow-tests'));
  assert.ok(!args.includes('--read-only'));
});
test('invalid modes, blank tasks and artifacts inside the target are rejected', () => {
  assert.throws(() => buildArgs({ ...input, mode: 'automatic' }, {}, runsRoot));
  assert.throws(() => buildArgs({ ...input, task: ' ' }, {}, runsRoot));
  assert.throws(() => buildArgs(input, {}, project), /仓库之外/);
  assert.throws(() => buildArgs(input, {}, path.join(project, 'runs')), /仓库之外/);
  assert.doesNotThrow(() => buildArgs(input, {}, project + '-runs'));
});
test('only complete JSONL events are published', () => {
  assert.deepEqual(parseEvents('{"event":"started"}\n{"event":'), [{ event: 'started' }]);
  assert.deepEqual(parseEvents('{"event":"started"}'), []);
  assert.throws(() => parseEvents('not json\n'));
});
test('historical verification and recovery status remain separate', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-history-'));
  try {
    const id = '20261014T000000Z-abcdef123456';
    const directory = path.join(root, id);
    await fs.mkdir(directory);
    await fs.writeFile(path.join(directory, 'summary.json'), JSON.stringify({ status: 'verified' }));
    await fs.writeFile(path.join(directory, 'manifest.json'), JSON.stringify({ version: 1, transactions: [{ status: 'rolled_back', files: [{ path: 'src/example.py', state: 'restored' }] }] }));
    const state = await snapshot(root, id);
    assert.equal(state.summary.status, 'verified');
    assert.equal(state.recovery.status, 'restored');
    await fs.unlink(path.join(directory, 'summary.json'));
    const interrupted = await snapshot(root, id);
    assert.equal(interrupted.summary, null);
    assert.equal(interrupted.recovery.status, 'restored');
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
test('unfinished and conflicting manifests stay visible for recovery', () => {
  const manifest = status => ({ version: 1, transactions: [{ status, files: [{ path: 'src/example.py', state: 'writing' }] }] });
  assert.equal(recoveryState(manifest('prepared')).status, 'pending');
  assert.equal(recoveryState(manifest('rollback_failed')).status, 'conflict');
  assert.equal(recoveryState(manifest('applied')).status, 'applied');
  assert.equal(recoveryState({ version: 1, transactions: [null] }).status, 'invalid');
});
test('history filters run IDs and snapshot rejects traversal', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-panel-'));
  try {
    const id = '20261014T000000Z-abcdef123456';
    await fs.mkdir(path.join(root, id));
    await fs.mkdir(path.join(root, 'other'));
    await fs.writeFile(path.join(root, id, 'events.jsonl'), '{"event":"started"}\n{"event":');
    assert.deepEqual(await listRuns(root), [id]);
    assert.equal((await snapshot(root, id)).events.length, 1);
    await assert.rejects(snapshot(root, '../other'), /无效/);
    await fs.writeFile(path.join(root, id, 'report.md'), Buffer.alloc(8 * 1024 * 1024 + 1));
    await assert.rejects(snapshot(root, id), /读取限制/);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
test('real readonly Mock CLI artifacts are readable without modifying the fixture', async () => {
  const repo = path.resolve(__dirname, '../../..');
  const python = process.env.MINI_AGENT_TEST_PYTHON || path.join(repo, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const fixture = path.join(repo, 'examples/buggy_project');
  const source = path.join(fixture, 'src/calculator.py');
  const before = await fs.readFile(source);
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-panel-cli-'));
  try {
    const args = buildArgs({ project: fixture, task: '修复 average([])', model: 'mock', mode: 'readonly' }, {}, root);
    const result = spawnSync(python, args, { cwd: repo, encoding: 'utf8', timeout: 20000, env: { ...process.env, PYTHONPATH: path.join(repo, 'src'), PYTHONIOENCODING: 'utf-8' } });
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr);
    const ids = await listRuns(root);
    assert.equal(ids.length, 1);
    const state = await snapshot(root, ids[0]);
    assert.equal(state.summary.status, 'proposed');
    assert.ok(state.hasDiff && state.hasReport);
    assert.ok(state.events.some(e => e.event === 'finished'));
    assert.deepEqual(await fs.readFile(source), before);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
