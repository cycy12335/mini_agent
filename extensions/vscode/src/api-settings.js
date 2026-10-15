'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const PROFILE = 'miniAgent.apiProfile';
const SECRET = 'miniAgent.apiKey';
const KEY_ENV = 'MINI_AGENT_PANEL_API_KEY';

function validateProfile(input) {
  if (!input || typeof input !== 'object') throw new Error('请填写 API 配置。');
  const { endpoint, model, protocol } = input;
  let url;
  try { url = new URL(endpoint); } catch { throw new Error('请输入完整的 API 请求地址。'); }
  if (typeof endpoint !== 'string' || endpoint.length > 2048 || /[\s\x00-\x20]/.test(endpoint) || url.username || url.password || url.search || url.hash ||
      !(url.protocol === 'https:' || url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) {
    throw new Error('API 地址必须为 HTTPS（本机允许 HTTP），且不能包含凭据、查询参数或片段。');
  }
  if (typeof model !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,199}$/.test(model)) throw new Error('请输入有效的模型 ID。');
  if (!['responses', 'chat_completions'].includes(protocol)) throw new Error('请选择有效的 API 协议。');
  const budget = Number(input.max_run_cost_cny);
  if (!Number.isFinite(budget) || budget <= 0 || budget > 0.5) throw new Error('单次预算须大于 0 且不超过 ¥0.5。');
  const result = { endpoint, model, protocol, api_key_env: KEY_ENV, max_run_cost_cny: budget };
  const prices = ['input_price_cny_per_million', 'output_price_cny_per_million'];
  const present = prices.map(key => input[key] !== '' && input[key] != null);
  if (present[0] !== present[1]) throw new Error('输入和输出单价需要一起填写，或一起留空。');
  if (present[0]) for (const key of prices) {
    const price = Number(input[key]);
    if (!Number.isFinite(price) || price <= 0) throw new Error('模型单价必须为正数。');
    result[key] = price;
  }
  if (input.reasoning_effort) {
    if (!['none', 'low', 'medium', 'high', 'max'].includes(input.reasoning_effort)) throw new Error('无效的推理强度。');
    result.reasoning_effort = input.reasoning_effort;
  }
  return result;
}

class ApiSettings {
  constructor(context) { this.context = context; }
  async base(configPath) {
    if (!configPath) return {};
    const buffer = await fs.readFile(configPath);
    if (buffer.length > 65536) throw new Error('API 配置文件过大。');
    try { return JSON.parse(buffer.toString('utf8').replace(/^\uFEFF/, '')); }
    catch { throw new Error('无法读取现有 API 配置，请检查 JSON 格式。'); }
  }
  async view(configPath) {
    const saved = this.context.globalState.get(PROFILE);
    const api = saved || (await this.base(configPath)).api || {};
    // Only public, editable fields cross into the Webview. Never return a key.
    const profile = {};
    for (const key of ['endpoint', 'model', 'protocol', 'reasoning_effort', 'max_run_cost_cny', 'input_price_cny_per_million', 'output_price_cny_per_million']) {
      if (api[key] !== undefined) profile[key] = api[key];
    }
    const models = [...new Set([api.model, ...(this.context.globalState.get('miniAgent.apiModels') || [])].filter(value => typeof value === 'string'))].slice(0, 12);
    return { type: 'api-settings', profile, models, panelManaged: Boolean(saved), hasKey: Boolean(await this.context.secrets.get(SECRET)) };
  }
  async save(input, key) {
    const profile = validateProfile(input);
    if (typeof key !== 'string' || key.length > 4096 || key && /[^\x21-\x7e]/.test(key)) throw new Error('API Key 格式无效，请检查空格或换行。');
    if (!key && !await this.context.secrets.get(SECRET)) throw new Error('首次保存请填写 API Key。');
    if (key) await this.context.secrets.store(SECRET, key);
    await this.context.globalState.update(PROFILE, profile);
    await this.context.globalState.update('miniAgent.apiModels', [...new Set([profile.model, ...(this.context.globalState.get('miniAgent.apiModels') || [])])].slice(0, 12));
  }
  async clear() {
    await this.context.secrets.delete(SECRET);
    await this.context.globalState.update(PROFILE, undefined);
  }
  async prepare(settings, storage) {
    const profile = this.context.globalState.get(PROFILE);
    if (!profile) return { settings, env: {} };
    const api = validateProfile(profile);
    const key = await this.context.secrets.get(SECRET);
    if (!key) throw new Error('面板 API Key 未配置，请在“模型与 API”中保存。');
    const base = await this.base(settings.configPath);
    const config = Object.fromEntries(Object.entries(base).filter(([name, value]) => name !== 'api' && typeof value === 'number'));
    config.api = { ...api };
    for (const name of ['max_output_tokens', 'max_response_bytes', 'max_retries']) {
      if (Number.isInteger(base.api?.[name])) config.api[name] = base.api[name];
    }
    // Isolated launch directory prevents legacy credential files overriding SecretStorage.
    const directory = path.join(storage, 'api-launch', crypto.randomBytes(12).toString('hex'));
    await fs.mkdir(directory, { recursive: true });
    const configPath = path.join(directory, 'agent.config.json');
    try { await fs.writeFile(configPath, JSON.stringify(config), { flag: 'wx' }); }
    catch (error) { await fs.rm(directory, { recursive: true, force: true }); throw error; }
    return { settings: { ...settings, configPath }, env: { [KEY_ENV]: key },
      cleanup: () => fs.rm(directory, { recursive: true, force: true }) };
  }
}
module.exports = { ApiSettings, validateProfile, KEY_ENV };
