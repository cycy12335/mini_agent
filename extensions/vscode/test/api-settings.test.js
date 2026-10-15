'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { ApiSettings, validateProfile, KEY_ENV } = require('../src/api-settings');
const profile = { endpoint: 'https://example.invalid/v1/chat/completions', model: 'custom-model', protocol: 'chat_completions', max_run_cost_cny: 0.5 };
function fixture() {
  const state = new Map(), secrets = new Map();
  const api = new ApiSettings({ globalState: { get: key => state.get(key), update: async (key, value) => state.set(key, value) },
    secrets: { get: async key => secrets.get(key), store: async (key, value) => secrets.set(key, value), delete: async key => secrets.delete(key) } });
  return { api, state, secrets };
}
test('API settings validate endpoint, model, protocol, budget and paired prices', () => {
  for (const invalid of [
    { endpoint: 'http://remote.invalid' }, { endpoint: 'https://user:secret@example.invalid' },
    { endpoint: 'https://example.invalid?key=secret' }, { model: 'bad model' }, { protocol: 'other' },
    { max_run_cost_cny: 1 }, { input_price_cny_per_million: 2 },
    { input_price_cny_per_million: -1, output_price_cny_per_million: 2 }
  ]) assert.throws(() => validateProfile({ ...profile, ...invalid }));
  assert.equal(validateProfile({ ...profile, endpoint: 'http://127.0.0.1:8000/chat' }).model, 'custom-model');
});
test('Key stays in SecretStorage; blank preserves it; clear returns to legacy configuration', async () => {
  const { api, state, secrets } = fixture();
  await assert.rejects(api.save(profile, ''), /Key/);
  await api.save(profile, 'unit-test-secret');
  await api.save({ ...profile, model: 'second-model' }, '');
  const view = await api.view('');
  assert.equal(view.hasKey, true);
  assert.equal(view.profile.model, 'second-model');
  assert.deepEqual(view.models, ['second-model', 'custom-model']);
  assert.ok(!JSON.stringify([...state]).includes('unit-test-secret'));
  assert.ok(!JSON.stringify(view).includes('unit-test-secret'));
  await assert.rejects(api.save(profile, 'key with spaces'), /Key/);
  await api.clear();
  assert.equal(secrets.size, 0);
  assert.equal((await api.view('')).panelManaged, false);
});
test('real Python core loads panel model and environment Key without credential files overriding it', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'mini-api-'));
  const { api } = fixture();
  let prepared;
  try {
    const configPath = path.join(directory, 'base.json');
    await fs.writeFile(configPath, JSON.stringify({ max_actions: 7, api: { ...profile, max_output_tokens: 2048 } }));
    await fs.writeFile(path.join(directory, 'agent.credentials.local.json'), JSON.stringify({ api_key: 'old-key' }));
    await api.save(profile, 'unit-test-secret');
    prepared = await api.prepare({ configPath, pythonPath: 'python' }, directory);
    const content = await fs.readFile(prepared.settings.configPath, 'utf8');
    assert.ok(!content.includes('unit-test-secret'));
    assert.equal(JSON.parse(content).max_actions, 7);
    assert.equal(JSON.parse(content).api.max_output_tokens, 2048);
    const root = path.resolve(__dirname, '../../..');
    const python = path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
    const script = 'import sys; from pathlib import Path; from mini_agent.config import load_config; from mini_agent.adapters.http_api import load_api_key; c=load_config(Path(sys.argv[1])); assert c.api.model=="custom-model"; assert load_api_key(c.api)=="unit-test-secret"; print("core settings verified")';
    const result = await promisify(execFile)(python, ['-c', script, prepared.settings.configPath], {
      cwd: path.dirname(prepared.settings.configPath), env: { ...process.env, ...prepared.env, PYTHONPATH: path.join(root, 'src') }
    });
    assert.match(result.stdout, /core settings verified/);
    assert.equal(prepared.env[KEY_ENV], 'unit-test-secret');
    await prepared.cleanup();
    await assert.rejects(fs.stat(prepared.settings.configPath), { code: 'ENOENT' });
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});
