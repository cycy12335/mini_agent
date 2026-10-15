'use strict';
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const { StringDecoder } = require('node:string_decoder');

class AgentProcess extends EventEmitter {
  constructor(python, args, options, spawnProcess = spawn) {
    super();
    this.child = spawnProcess(python, args, { ...options, shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    this.buffer = '';
    this.seq = 0;
    this.closed = false;
    const decoder = new StringDecoder('utf8');
    this.child.stdout.on('data', chunk => {
      this.buffer += decoder.write(chunk);
      if (Buffer.byteLength(this.buffer) > 8 * 1024 * 1024) {
        this.emit('fault', 'Agent 协议消息超出读取限制。');
        this.cancel(); this.child.stdin.end(); this.buffer = ''; return;
      }
      let newline;
      while ((newline = this.buffer.indexOf('\n')) !== -1) {
        const line = this.buffer.slice(0, newline); this.buffer = this.buffer.slice(newline + 1);
        try {
          const event = JSON.parse(line);
          if (event.version !== 1 || !Number.isInteger(event.seq) || event.seq <= this.seq || typeof event.type !== 'string') throw new Error();
          this.seq = event.seq;
          this.emit('message', event);
        } catch { this.emit('fault', 'Agent 协议不兼容，请更新 Python Agent。'); this.cancel(); }
      }
    });
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', text => this.emit('diagnostic', text));
    this.child.stdin.on('error', () => this.emit('fault', 'Agent 命令通道已关闭。'));
    this.child.on('error', error => this.emit('fault', `无法启动 Agent：${error.code || error.name}`));
    this.child.on('close', (code, signal) => {
      this.closed = true;
      if (this.buffer.trim()) this.emit('fault', 'Agent 输出中断，最后一条事件不完整。');
      this.emit('exit', code, signal);
    });
  }

  send(command) {
    if (this.closed || !this.child.stdin.writable) throw new Error('Agent 已退出。');
    this.child.stdin.write(JSON.stringify({ version: 1, ...command }) + '\n');
  }
  approve(id, approved) {
    if (typeof id !== 'string' || typeof approved !== 'boolean') throw new Error('无效的审批回复。');
    this.send({ type: 'approval', approval_id: id, approved });
  }
  cancel() { if (!this.closed && this.child.stdin.writable) this.send({ type: 'cancel' }); }
  dispose() { this.cancel(); this.child.stdin.end(); }
}

module.exports = { AgentProcess };
