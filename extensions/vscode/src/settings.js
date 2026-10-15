'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');

// Local packaging may provide paths only. Explicit VS Code settings always win.
async function settingsFor(config, extensionPath) {
  let defaults = {};
  try { defaults = JSON.parse(await fs.readFile(path.join(extensionPath, 'local-runtime.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const settings = {};
  for (const [key, fallback] of Object.entries({ pythonPath: 'python', configPath: '', testPythonPath: '' })) {
    const inspected = config.inspect?.(key);
    const explicit = inspected && ['globalValue', 'workspaceValue', 'workspaceFolderValue'].some(level => inspected[level] !== undefined);
    settings[key] = explicit ? config.get(key, fallback) : defaults[key] || config.get(key, fallback);
  }
  return settings;
}
module.exports = { settingsFor };
