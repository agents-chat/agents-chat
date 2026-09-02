import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const command = process.platform === 'win32'
  ? { file: 'cmd.exe', args: ['/d', '/s', '/c', path.join(root, 'Start Agents Chat on Windows.cmd')] }
  : process.platform === 'darwin'
    ? { file: path.join(root, 'Start Agents Chat on Mac.command'), args: [] }
    : null;

if (!command) {
  console.error('Agents Chat Community currently supports macOS and Windows.');
  process.exit(1);
}

const result = spawnSync(command.file, command.args, { cwd: root, stdio: 'inherit' });
if (result.error) {
  console.error(`Unable to start Agents Chat: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
