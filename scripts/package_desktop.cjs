const { createRequire } = require('node:module');
const { resolve, join } = require('node:path');

const [platform, arch, backend, output] = process.argv.slice(2);
if (!['win', 'mac'].includes(platform) || !['x64', 'arm64'].includes(arch) || !backend || !output) {
  process.stderr.write('Usage: node scripts/package_desktop.cjs win|mac x64|arm64 BACKEND_DIR OUTPUT_DIR\n');
  process.exit(2);
}
const projectDir = resolve(__dirname, '../fronted');
const { Arch, Platform, build } = createRequire(join(projectDir, 'package.json'))('electron-builder');
const target = platform === 'win' ? Platform.WINDOWS : Platform.MAC;
const formats = platform === 'win' ? ['nsis', 'portable'] : ['dmg', 'zip'];

build({
  projectDir,
  targets: target.createTarget(formats, Arch[arch]),
  publish: 'never',
  config: {
    extends: join(projectDir, 'electron-builder.yml'),
    directories: { output: resolve(output) },
    extraResources: [{ from: resolve(backend), to: 'backend' }],
    npmRebuild: false,
  },
}).catch((error) => {
  process.stderr.write(String(error.stack || error) + '\n');
  process.exitCode = 1;
});
