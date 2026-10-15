'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

function buildInsights(events, diagnosis, summary) {
  const locations = {};
  function location(key, item) {
    if (item && typeof item.path === 'string' && Number.isInteger(item.line) && item.line > 0 && /^[a-f0-9]{64}$/.test(item.sha256 || '')) {
      locations[key] = { path: item.path, line: item.line, end_line: item.end_line || item.line, sha256: item.sha256 };
      return key;
    }
    return null;
  }
  const definitions = new Map(), queries = [];
  for (const event of events) {
    if (event.event !== 'tool_result' || !event.result?.ok || !event.name?.startsWith('find_')) continue;
    const data = event.result.data;
    if (!data || !Array.isArray(data.matches)) continue;
    for (const def of [...(data.definitions || []), ...data.matches.filter(item => item.id && item.symbol)]) definitions.set(def.id, def);
    if (['find_callers', 'find_callees'].includes(event.name)) queries.push({ data, truncated: event.result.truncated });
  }
  const nodes = new Map(), edges = new Map();
  let limited = false;
  const node = (id, fallback = null) => {
    if (nodes.has(id)) return nodes.get(id);
    if (nodes.size >= 60) { limited = true; return null; }
    const def = definitions.get(id);
    const item = { id, label: def?.symbol || id, location: location('symbol:' + id, def || fallback), uncertain: !def || Boolean(def.decorated || def.conditional) };
    nodes.set(id, item); return item;
  };
  let coverageIncomplete = false;
  for (const { data, truncated } of queries) {
    coverageIncomplete ||= data.coverage?.complete === false || Boolean(data.ambiguous);
    limited ||= Boolean(truncated);
    for (const match of data.matches) {
      if (match.kind !== 'call' || typeof match.owner !== 'string') continue;
      const from = node(match.owner, match);
      const targets = match.targets?.length ? match.targets : [`unresolved:${match.path}:${match.line}:${match.expression}`];
      for (const target of targets) {
        const to = node(target);
        if (!from || !to) continue;
        const unresolved = target.startsWith('unresolved:');
        if (unresolved) to.label = '未解析：' + match.expression;
        const uncertain = unresolved || Boolean(match.uncertain || match.conditional || data.ambiguous || data.coverage?.complete === false);
        const key = `${from.id}|${to.id}|${match.path}:${match.line}`;
        if (edges.has(key)) continue;
        if (edges.size >= 80) { limited = true; continue; }
        edges.set(key, { from: from.id, to: to.id, uncertain, reason: match.reason || (uncertain ? 'candidate' : 'static_binding'), location: location('call:' + key, match) });
      }
    }
  }
  const evidence = (diagnosis?.evidence || []).slice(-100).map(item => ({
    id: item.id, kind: item.kind, uncertain: Boolean(item.uncertain), revision: item.revision,
    label: item.kind === 'code' ? `${item.path}:${item.line}` : `${item.phase || ''} 测试 · 退出码 ${item.exit_code}`,
    excerpt: item.excerpt || '', artifact: item.artifact,
    location: item.kind === 'code' ? location('evidence:' + item.id, item) : null
  }));
  const diagnostic = summary?.diagnosis || diagnosis?.summary;
  const root = diagnostic?.root_cause || diagnosis?.root_cause;
  const costEvent = [...events].reverse().find(event => event.event === 'model_cost' && event.budget);
  const cost = summary?.api_cost || costEvent?.budget || null;
  const diagnosticStatus = diagnostic?.status || (root ? 'evidence_supported' : 'unconfirmed');
  return {
    graph: { nodes: [...nodes.values()], edges: [...edges.values()], limited, coverageIncomplete,
      notice: '仅显示记录时查询到的静态关系；实线不代表运行验证，虚线表示候选或未解析关系。文件变化后旧关系可能过期。' },
    diagnosis: { status: diagnosticStatus, problem: diagnosis?.problem || null, root,
      hypotheses: (diagnosis?.hypotheses || []).slice(0, 32), evidence,
      unresolved: diagnostic?.unresolved || [], evidenceTruncated: Boolean(diagnosis?.evidence_truncated || diagnosis?.evidence?.length > 100) },
    budget: cost ? { currency: cost.currency, limit: cost.limit_cny, accounted: cost.accounted_upper_estimate_cny,
      remaining: cost.remaining_cny, uncertainRequests: cost.uncertain_requests,
      requests: cost.attempts?.length || 0, billingVerified: false } : null,
    actions: summary?.actions ?? events.filter(event => event.event === 'action').length,
    locations
  };
}

async function checkedSource(project, location) {
  if (typeof project !== 'string' || !path.isAbsolute(project) || !location) throw new Error('没有可用的代码位置。');
  const relative = location.path;
  const parts = typeof relative === 'string' ? relative.replaceAll('\\', '/').split('/') : [];
  if (!parts.length || path.isAbsolute(relative) || /^[A-Za-z]:/.test(relative) || parts.some(part => !part || part === '..' || part.startsWith('.') || /[:\x00-\x1f]/.test(part) || /credential|secret|password|api[_-]?key|private[_-]?key|id_rsa/i.test(part))) throw new Error('代码路径不在允许范围内。');
  if (!['.py', '.pyi', '.md', '.rst', '.txt', '.toml', '.json', '.yaml', '.yml', '.ini', '.cfg'].includes(path.extname(relative).toLowerCase())) throw new Error('代码位置不是允许的文本文件。');
  if (!Number.isInteger(location.line) || location.line < 1 || !/^[a-f0-9]{64}$/.test(location.sha256 || '')) throw new Error('代码位置缺少有效行号或内容哈希。');
  for (let component = path.resolve(project); ; component = path.dirname(component)) {
    if ((await fs.lstat(component)).isSymbolicLink()) throw new Error('不跳转链接仓库。');
    if (path.dirname(component) === component) break;
  }
  const root = await fs.realpath(project);
  const file = path.join(root, ...parts);
  for (let index = 1; index <= parts.length; index++) {
    if ((await fs.lstat(path.join(root, ...parts.slice(0, index)))).isSymbolicLink()) throw new Error('不跳转链接文件。');
  }
  const resolved = await fs.realpath(file);
  const boundary = path.relative(root, resolved);
  if (boundary === '..' || boundary.startsWith('..' + path.sep) || path.isAbsolute(boundary)) throw new Error('代码位置越过仓库边界。');
  const handle = await fs.open(resolved, 'r');
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size > 1024 * 1024) throw new Error('代码文件超出读取范围。');
    const bytes = Buffer.alloc(1024 * 1024 + 1);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead > 1024 * 1024 || crypto.createHash('sha256').update(bytes.subarray(0, bytesRead)).digest('hex') !== location.sha256) throw new Error('代码已变化，历史位置可能过期。请重新收集证据。');
  } finally { await handle.close(); }
  return resolved;
}

module.exports = { buildInsights, checkedSource };
