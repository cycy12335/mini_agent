'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { buildInsights, checkedSource } = require('../src/insights');
const { snapshot } = require('../src/runtime');
const { spawnSync } = require('node:child_process');
const digest = text => crypto.createHash('sha256').update(text).digest('hex');
const def = { id: 'src/a.py::caller@1', symbol: 'a.caller', path: 'src/a.py', line: 1, sha256: digest('def caller(): pass\n') };

test('static calls, unresolved calls and incomplete coverage keep uncertainty', () => {
  const event = { event: 'tool_result', name: 'find_callees', result: { ok: true, data: {
    definitions: [def], matches: [
      { owner: def.id, kind: 'call', path: 'src/a.py', line: 1, sha256: def.sha256, targets: ['src/b.py::target@1'], expression: 'target', uncertain: false },
      { owner: def.id, kind: 'call', path: 'src/a.py', line: 2, sha256: def.sha256, targets: [], expression: 'dynamic', uncertain: true }
    ], coverage: { complete: true }
  } } };
  const info = buildInsights([event], null, null);
  assert.equal(info.graph.edges.length, 2);
  assert.equal(info.graph.edges[0].uncertain, false);
  assert.equal(info.graph.edges[1].uncertain, true);
  assert.ok(info.graph.nodes.some(node => node.label === '未解析：dynamic'));
  assert.ok(info.locations['symbol:' + def.id]);
  event.result.data.coverage.complete = false;
  assert.ok(buildInsights([event], null, null).graph.edges.every(edge => edge.uncertain));
});
test('diagnostic status and client cost estimate do not imply verification', () => {
  const info = buildInsights([{ event: 'model_cost', budget: { currency: 'CNY', limit_cny: '0.5', accounted_upper_estimate_cny: '0.1', remaining_cny: '0.4', uncertain_requests: 1, attempts: [{}] } }],
    { root_cause: { conclusion: 'claim' }, evidence: [{ ...def, id: 'E1', kind: 'code' }], hypotheses: [] },
    { diagnosis: { status: 'supported_before_edit', root_cause: { conclusion: 'claim' }, unresolved: ['Recheck current source'] } });
  assert.equal(info.diagnosis.status, 'supported_before_edit');
  assert.equal(info.budget.billingVerified, false);
  assert.equal(info.budget.uncertainRequests, 1);
  assert.equal(info.diagnosis.evidence[0].location, 'evidence:E1');
});
test('source jump rejects traversal, sensitive paths, missing hashes and stale evidence', async () => {
  const project = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-jump-'));
  try {
    const content = 'def caller(): pass\n';
    await fs.mkdir(path.join(project, 'src'));
    await fs.writeFile(path.join(project, 'src/a.py'), content);
    const ref = { path: 'src/a.py', line: 1, sha256: digest(content) };
    assert.equal(await checkedSource(project, ref), path.join(await fs.realpath(project), 'src/a.py'));
    await assert.rejects(checkedSource(project, { ...ref, path: '../outside.py' }), /允许范围/);
    await assert.rejects(checkedSource(project, { ...ref, path: '.env' }), /允许范围/);
    await assert.rejects(checkedSource(project, { ...ref, path: 'credentials.json' }), /允许范围/);
    await assert.rejects(checkedSource(project, { ...ref, sha256: null }), /哈希/);
    await fs.writeFile(path.join(project, 'src/a.py'), content + '# changed');
    await assert.rejects(checkedSource(project, ref), /代码已变化/);
  } finally { await fs.rm(project, { recursive: true, force: true }); }
});
test('actual offline core diagnosis produces a linked cross-file graph and evidence', async () => {
  const repo = path.resolve(__dirname, '../../..');
  const python = path.join(repo, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const result = spawnSync(python, [path.join(repo, 'scripts/prepare_visual_diagnosis.py')], {
    cwd: repo, encoding: 'utf8', timeout: 20000, env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  const { directory } = JSON.parse(await fs.readFile(path.join(repo, '.mini-agent/visual-diagnosis/latest.json'), 'utf8'));
  const state = await snapshot(path.dirname(directory), path.basename(directory));
  assert.equal(state.insights.diagnosis.status, 'evidence_supported');
  assert.ok(state.insights.graph.edges.length > 0);
  assert.ok(state.insights.graph.nodes.some(node => node.label.includes('checkout')));
  assert.ok(state.insights.graph.nodes.some(node => node.label.includes('mean')));
  assert.ok(state.insights.diagnosis.evidence.some(item => item.label.includes('maths.py')));
});
