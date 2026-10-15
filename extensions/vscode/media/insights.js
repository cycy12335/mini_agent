'use strict';
(() => {
  let current = null;
  let send = null;
  let fingerprint = '';
  const $ = id => document.getElementById(id);
  const text = (tag, content, className) => { const element = document.createElement(tag); element.textContent = content; if (className) element.className = className; return element; };
  function sourceButton(label, ref, runId) {
    const button = text('button', label, 'secondary'); button.disabled = !ref;
    button.addEventListener('click', () => send('source', { id: runId, ref })); return button;
  }
  function render(state, transmit) {
    current = state; send = transmit;
    const next = JSON.stringify({ id: state?.id, insights: state?.insights });
    if (next === fingerprint) return; // Preserve graph focus while polling unchanged artifacts.
    fingerprint = next;
    const info = state?.insights;
    const diagnosis = info?.diagnosis;
    const labels = { unconfirmed: '尚未确认根因', evidence_supported: '结论有证据引用支持', supported_before_edit: '修改前证据支持，当前内容需要复核' };
    $('diagnosis-status').textContent = diagnosis ? labels[diagnosis.status] || diagnosis.status : '尚无定位记录';
    $('root-cause').textContent = diagnosis?.root?.conclusion || '当前尚无根因结论。';
    $('hypotheses').replaceChildren();
    if (diagnosis?.problem) {
      const problem = diagnosis.problem;
      $('hypotheses').append(text('p', `预期：${problem.expected_behavior}\n触发：${problem.trigger}\n实际：${problem.observed_behavior}`, 'preserve-lines'));
    }
    const outcomes = { supported: '支持', rejected: '排除', inconclusive: '不确定', pending: '等待检查' };
    for (const hypothesis of diagnosis?.hypotheses || []) {
      const card = text('div', '', 'hypothesis');
      card.append(text('strong', `${hypothesis.id} · ${outcomes[hypothesis.status] || hypothesis.status}`), text('p', hypothesis.claim));
      for (const check of hypothesis.checks || []) card.append(text('p', `${outcomes[check.outcome] || check.outcome}：${check.finding}`, 'hint'));
      const ids = [...new Set([...(hypothesis.evidence_ids || []), ...(hypothesis.checks || []).flatMap(check => check.evidence_ids || [])])];
      const links = text('div', '', 'row actions');
      for (const id of ids) {
        const evidence = diagnosis.evidence.find(item => item.id === id);
        if (evidence?.kind === 'code') links.append(sourceButton(id + ' · ' + evidence.label, evidence.location, state.id));
        else links.append(text('span', id + (evidence ? ' · 执行证据' : ' · 未显示'), 'hint'));
      }
      card.append(links); $('hypotheses').append(card);
    }
    $('evidence').replaceChildren();
    for (const evidence of diagnosis?.evidence || []) {
      const card = text('div', '', 'hypothesis');
      card.append(text('strong', `${evidence.id} · ${evidence.label}${evidence.uncertain ? ' · 不确定' : ''}`));
      if (evidence.excerpt) card.append(text('pre', evidence.excerpt));
      if (evidence.kind === 'code') card.append(sourceButton('打开代码位置', evidence.location, state.id));
      else if (evidence.artifact) {
        const button = text('button', '查看执行记录', 'secondary');
        button.addEventListener('click', () => send('artifact', { id: state.id, name: evidence.artifact })); card.append(button);
      }
      $('evidence').append(card);
    }
    $('unresolved').textContent = (diagnosis?.unresolved || []).join('\n') || '尚无最终未确认项记录。';
    if (diagnosis?.evidenceTruncated) $('unresolved').append('\n证据记录已达到预算，列表可能不完整。');
    const focus = $('graph-focus').value;
    $('graph-focus').replaceChildren(text('option', '全部相关关系'));
    $('graph-focus').firstChild.value = '';
    for (const node of info?.graph.nodes || []) { const option = text('option', node.label); option.value = node.id; $('graph-focus').append(option); }
    if ((info?.graph.nodes || []).some(node => node.id === focus)) $('graph-focus').value = focus;
    renderGraph();
    const budget = info?.budget;
    const unit = budget?.currency === 'CNY' ? '¥' : budget?.currency || '币种未知 ';
    const money = value => Number.isFinite(Number(value)) ? Number(value).toFixed(6) : '未知';
    $('budget').textContent = budget
      ? `动作 ${info.actions} · 模型请求 ${budget.requests}\n费用预留/计入 ${unit}${money(budget.accounted)} / 上限 ${unit}${money(budget.limit)}\n剩余 ${unit}${money(budget.remaining)} · 用量未确定请求 ${budget.uncertainRequests}`
      : `动作 ${info?.actions || 0} · 没有模型费用记录（离线任务或尚未请求模型）。`;
  }
  function renderGraph() {
    const graph = current?.insights?.graph;
    const focus = $('graph-focus').value;
    const candidates = graph?.edges || [];
    const relevant = focus ? candidates.filter(edge => edge.from === focus || edge.to === focus) : candidates;
    const ids = new Set(relevant.flatMap(edge => [edge.from, edge.to]));
    if (focus) ids.add(focus);
    const nodes = (graph?.nodes || []).filter(node => ids.has(node.id)).slice(0, 24);
    const visible = new Set(nodes.map(node => node.id));
    const edges = relevant.filter(edge => visible.has(edge.from) && visible.has(edge.to));
    $('call-graph').replaceChildren(); $('call-sites').replaceChildren();
    $('graph-note').textContent = (graph?.notice || '') + (graph?.coverageIncomplete ? ' 查询范围不完整或有歧义。' : '') + (graph?.limited || ids.size > 24 ? ' 展示已截断，可选择符号聚焦。' : '');
    if (!nodes.length) { $('call-graph').append(text('p', '尚无调用查询记录。普通 Mock 不一定产生调用图，可导入离线定位示例。', 'hint')); return; }
    const ns = 'http://www.w3.org/2000/svg';
    const svgElement = (tag, attributes = {}, content = '') => {
      const element = document.createElementNS(ns, tag); for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value)); element.textContent = content; return element;
    };
    const incoming = new Set(edges.map(edge => edge.to));
    const rank = new Map();
    const queue = nodes.filter(node => !incoming.has(node.id)).map(node => node.id);
    if (!queue.length) queue.push(nodes[0].id);
    for (const id of queue) rank.set(id, 0);
    for (let index = 0; index < queue.length; index++) for (const edge of edges.filter(edge => edge.from === queue[index])) {
      if (!rank.has(edge.to)) { rank.set(edge.to, Math.min(3, rank.get(edge.from) + 1)); queue.push(edge.to); }
    }
    const rows = [0, 0, 0, 0], positions = new Map();
    for (const node of nodes) { const column = rank.get(node.id) || 0; positions.set(node.id, { x: column * 220 + 16, y: rows[column]++ * 90 + 16 }); }
    const width = (Math.max(...[...positions.values()].map(p => p.x)) + 200);
    const height = Math.max(...rows) * 90 + 32;
    const svg = svgElement('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', 'aria-label': '本次任务查询到的静态调用关系' });
    const defs = svgElement('defs');
    const marker = svgElement('marker', { id: 'call-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse' });
    marker.append(svgElement('path', { d: 'M 0 0 L 10 5 L 0 10 z', class: 'arrow' })); defs.append(marker); svg.append(defs);
    for (const edge of edges) {
      const from = positions.get(edge.from), to = positions.get(edge.to);
      const curve = edge.from === edge.to ? `M ${from.x + 184} ${from.y + 30} C ${from.x + 240} ${from.y - 20}, ${from.x - 56} ${from.y - 20}, ${from.x} ${from.y + 30}` : `M ${from.x + 184} ${from.y + 30} C ${from.x + 208} ${from.y + 30}, ${to.x - 24} ${to.y + 30}, ${to.x} ${to.y + 30}`;
      const path = svgElement('path', { d: curve, class: edge.uncertain ? 'edge uncertain' : 'edge', 'marker-end': 'url(#call-arrow)' });
      path.append(svgElement('title', {}, `${edge.uncertain ? '候选关系' : '静态绑定'} · ${edge.reason}`)); svg.append(path);
      if (edge.location) $('call-sites').append(sourceButton('调用点 · ' + edge.from.split('::').at(-1), edge.location, current.id));
    }
    for (const node of nodes) {
      const position = positions.get(node.id);
      const group = svgElement('g', { transform: `translate(${position.x},${position.y})`, class: 'graph-node' + (node.uncertain ? ' candidate' : ''), role: node.location ? 'button' : 'img' });
      group.append(svgElement('rect', { width: 184, height: 60, rx: 6 }), svgElement('text', { x: 10, y: 26 }, node.label.length > 23 ? node.label.slice(0, 22) + '…' : node.label), svgElement('text', { x: 10, y: 46, class: 'node-note' }, node.uncertain ? '候选 / 未解析' : '静态定义'), svgElement('title', {}, node.label));
      if (node.location) {
        group.setAttribute('tabindex', '0'); group.addEventListener('click', () => send('source', { id: current.id, ref: node.location }));
        group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); send('source', { id: current.id, ref: node.location }); } });
      }
      svg.append(group);
    }
    $('call-graph').append(svg);
  }
  $('graph-focus').addEventListener('change', renderGraph);
  window.MiniAgentInsights = { render };
})();
