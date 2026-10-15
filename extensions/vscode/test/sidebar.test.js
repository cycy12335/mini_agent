'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { settingsFor } = require('../src/settings');

test('local installation defaults work outside the Agent repository; explicit settings win', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-settings-'));
  const fallback = { pythonPath: '/agent/python', configPath: '/agent/config.json' };
  try {
    await fs.writeFile(path.join(directory, 'local-runtime.json'), JSON.stringify(fallback));
    const config = { get: (_, value) => value, inspect: () => ({}) };
    assert.equal((await settingsFor(config, directory)).pythonPath, fallback.pythonPath);
    config.inspect = () => ({ workspaceValue: '' });
    config.get = key => key === 'pythonPath' ? '/custom/python' : '';
    assert.deepEqual(await settingsFor(config, directory), { pythonPath: '/custom/python', configPath: '', testPythonPath: '' });
    await fs.unlink(path.join(directory, 'local-runtime.json'));
    assert.equal((await settingsFor(config, directory)).pythonPath, '/custom/python');
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test('sidebar uses current workspace instead of a stale saved project and preserves chosen paths on state refresh', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: '', addEventListener() {} });
    return elements.get(id);
  };
  let message;
  const context = {
    document: { getElementById: element },
    acquireVsCodeApi: () => ({ getState: () => ({ project: '/old', task: 'bug' }), setState() {}, postMessage() {} }),
    window: { addEventListener: (_, listener) => { message = listener; } }
  };
  vm.runInNewContext(await fs.readFile(path.join(__dirname, '../media/panel.js'), 'utf8'), context);
  message({ data: { type: 'init', project: '/current' } });
  assert.equal(element('project').value, '/current');
  assert.equal(element('task').value, 'bug');
  message({ data: { type: 'project', project: '/chosen' } });
  assert.equal(element('project').value, '/chosen');
  message({ data: { type: 'workspace', project: '/new' } });
  assert.equal(element('project').value, '/new');
});

test('API form clears submitted Key and never persists it in Webview state', async () => {
  const elements = new Map(), sent = [], stored = [];
  let receive;
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: '', listeners: {},
      addEventListener(type, fn) { this.listeners[type] = fn; }, replaceChildren() {} });
    return elements.get(id);
  };
  vm.runInNewContext(await fs.readFile(path.join(__dirname, '../media/panel.js'), 'utf8'), {
    document: { getElementById: element, createElement: () => ({}) },
    acquireVsCodeApi: () => ({ getState: () => ({}), setState: state => stored.push(state), postMessage: msg => sent.push(msg) }),
    window: { addEventListener: (_, listener) => { receive = listener; } }
  });
  receive({ data: { type: 'api-settings', profile: { endpoint: 'https://example.invalid/chat', model: 'custom', protocol: 'chat_completions' }, models: ['custom'], hasKey: false } });
  element('api-key').value = 'form-test-secret';
  element('api-key').listeners.input();
  element('model').value = 'api';
  element('task-form').listeners.submit({ preventDefault() {} });
  assert.ok(!sent.some(msg => msg.type === 'start'), 'unsaved config blocks API launch');
  element('api-form').listeners.submit({ preventDefault() {} });
  const save = sent.find(msg => msg.type === 'save-api');
  assert.equal(save.key, 'form-test-secret');
  assert.equal(save.profile.model, 'custom');
  assert.equal(element('api-key').value, '');
  receive({ data: { type: 'api-settings', profile: save.profile, hasKey: true, saved: true, panelManaged: true } });
  element('task-form').listeners.submit({ preventDefault() {} });
  assert.ok(sent.some(msg => msg.type === 'start'));
  assert.ok(!JSON.stringify(stored).includes('form-test-secret'));
});
