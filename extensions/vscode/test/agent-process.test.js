'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { AgentProcess } = require('../src/agent-process');
const { buildArgs, listRuns, snapshot } = require('../src/runtime');

for (const action of ['approve', 'cancel']) test(`real Python bridge: ${action} at proposal`, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-bridge-'));
  const repo = path.resolve(__dirname, '../../..');
  let process;
  try {
    const project = path.join(root, 'project');
    await fs.cp(path.join(repo, 'examples/buggy_project'), project, { recursive: true, filter: source => !source.includes('__pycache__') && !source.includes('.pytest_cache') });
    const source = path.join(project, 'src/calculator.py');
    const original = await fs.readFile(source);
    const runs = path.join(root, 'runs');
    const args = buildArgs({ project, task: '修复 average([])', model: 'mock', mode: 'approve' }, {}, runs);
    args.splice(2, 2, 'mini_agent.panel');
    const python = path.join(repo, '.venv', global.process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    process = new AgentProcess(python, args, { cwd: repo, env: { ...global.process.env, PYTHONPATH: path.join(repo, 'src'), PYTHONIOENCODING: 'utf-8' } });
    const messages = [];
    const exitCode = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { process.dispose(); reject(new Error('Bridge timed out')); }, 15000);
      process.on('fault', reject);
      process.on('message', event => {
        messages.push(event);
        if (event.type === 'approval_requested') {
          if (action === 'approve') process.approve(event.approval_id, true);
          else process.cancel();
        }
      });
      process.on('exit', code => { clearTimeout(timer); resolve(code); });
    });
    assert.equal(exitCode, action === 'approve' ? 2 : 3);
    assert.ok(messages.some(e => e.type === 'proposal' && e.payload.files[0].path === 'src/calculator.py'));
    const state = await snapshot(runs, (await listRuns(runs))[0]);
    assert.equal(state.summary.status, action === 'approve' ? 'unverified' : 'cancelled');
    assert.equal((await fs.readFile(source)).equals(original), action !== 'approve');
  } finally {
    if (process && !process.closed) process.dispose();
    await fs.rm(root, { recursive: true, force: true });
  }
});
