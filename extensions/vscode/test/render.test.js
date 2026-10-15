'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('graph renderer creates SVG, preserves literal text and handles keyboard source jumps', () => {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.value = ''; this._text = ''; }
    set textContent(value) { this._text = String(value ?? ''); this.children = []; }
    get textContent() { return this._text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; this._text = ''; }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    get firstChild() { return this.children[0]; }
  }
  const elements = new Map();
  const document = {
    getElementById: id => { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); },
    createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)
  };
  const context = vm.createContext({ document, window: {} });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../media/insights.js'), 'utf8'), context);
  const sent = [];
  const state = { id: 'run-id', insights: {
    graph: { nodes: [{ id: 'caller', label: 'checkout', location: 'src:caller' }, { id: 'target', label: 'mean', location: 'src:target' }], edges: [{ from: 'caller', to: 'target', uncertain: true, reason: 'candidate', location: 'call:1' }], notice: 'Static candidates' },
    diagnosis: { status: 'supported_before_edit', root: { conclusion: '<script>literal claim</script>' }, hypotheses: [], evidence: [], unresolved: [] },
    budget: { requests: 1, limit: '0.5', accounted: '0.1', remaining: '0.4', uncertainRequests: 1 }, actions: 3
  } };
  context.window.MiniAgentInsights.render(state, (type, fields) => sent.push({ type, ...fields }));
  assert.equal(elements.get('root-cause').textContent, '<script>literal claim</script>');
  assert.match(elements.get('diagnosis-status').textContent, /修改前/);
  const svg = elements.get('call-graph').firstChild;
  assert.equal(svg.tag, 'svg');
  assert.ok(svg.attributes.viewBox);
  assert.ok(svg.children.some(child => child.tag === 'path' && child.attributes.class === 'edge uncertain'));
  context.window.MiniAgentInsights.render(state, (type, fields) => sent.push({ type, ...fields }));
  assert.equal(elements.get('call-graph').firstChild, svg, 'unchanged polling must preserve interactive nodes');
  const node = svg.children.find(child => child.tag === 'g');
  node.listeners.keydown({ key: 'Enter', preventDefault() {} });
  assert.deepEqual(sent.at(-1), { type: 'source', id: 'run-id', ref: 'src:caller' });
  elements.get('graph-focus').value = 'target';
  elements.get('graph-focus').listeners.change();
  assert.equal(elements.get('call-graph').firstChild.tag, 'svg');
  assert.match(elements.get('budget').textContent, /0\.100000/);
});
