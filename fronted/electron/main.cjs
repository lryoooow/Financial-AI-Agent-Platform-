// Electron 主进程：拉起 Python 后端 sidecar，开无原生边框窗口加载其本地 URL。
// 关键修复（根因 2）：frame:false 去掉 Windows 原生标题栏按钮，避免与 React 标题栏"两套按钮"共存；
// 窗口控制（最小化/最大化/关闭）改由 preload 暴露的 IPC 由应用内 TitleBar 触发。
// 图标统一（根因 4）：窗口/任务栏用 electron/icon.ico（随 electron/ 一并打包）。
const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');

let backend = null;
let win = null;
let quitting = false;
let restartingBackend = false;
let backendRestartAttempts = 0;

// 应用内窗口图标：electron/icon.ico 在 dev 与 packaged 下都随 main.cjs 同目录存在。
function windowIcon() {
  return path.join(__dirname, 'icon.ico');
}

function backendExe() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend', 'FinResearch.exe');
  }
  // dev：指向已打包的后端 exe（先在 backend 跑过 pyinstaller）
  return path.join(__dirname, '..', '..', 'backend', 'dist', 'FinResearch', 'FinResearch.exe');
}

// 产品名 1.2.2 起改为「景渊 JingYuan」，但用户数据目录仍沿用 FinResearch：
// 这里存着 SQLite（对话、报告）、模型密钥与 port.txt。改名会让老用户的数据凭空消失，
// 且必须与 backend/config.py 的 app_data_dir() 保持一致（两端靠 port.txt 握手）。
function appDataDir() {
  return path.join(os.homedir(), 'AppData', 'Roaming', 'FinResearch');
}

function backendLogFile() {
  return path.join(appDataDir(), 'backend.log');
}

function appendBackendLog(message) {
  try {
    fs.mkdirSync(appDataDir(), { recursive: true });
    fs.appendFileSync(backendLogFile(), `${new Date().toISOString()} ${message}\n`, 'utf8');
  } catch (_) {}
}

function logBackendChunk(prefix, chunk) {
  const text = chunk.toString('utf8').trim();
  if (text) appendBackendLog(`${prefix} ${text}`);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function portFile() {
  return path.join(appDataDir(), 'port.txt');
}

function clearStalePort() {
  try {
    fs.unlinkSync(portFile());
  } catch (_) {}
}

function readPort(retries = 240) {
  return new Promise((resolve, reject) => {
    let n = 0;
    const tick = () => {
      try {
        const p = parseInt(fs.readFileSync(portFile(), 'utf8').trim(), 10);
        if (p) return resolve(p);
      } catch (_) {}
      if (++n > retries) return reject(new Error('后端端口读取超时'));
      setTimeout(tick, 500);
    };
    tick();
  });
}

function waitHealth(port, retries = 240) {
  return new Promise((resolve, reject) => {
    let n = 0;
    const tick = () => {
      const req = http.get(`http://127.0.0.1:${port}/api/health`, (res) => {
        res.resume();
        if (res.statusCode === 200) resolve();
        else retry();
      });
      req.on('error', retry);
      function retry() {
        if (++n > retries) return reject(new Error('后端健康检查超时'));
        setTimeout(tick, 500);
      }
    };
    tick();
  });
}

function backendEnv() {
  const marketHosts = [
    'localhost',
    '127.0.0.1',
    '*.eastmoney.com',
    '*.push2.eastmoney.com',
    '*.push2his.eastmoney.com',
    '*.sina.com.cn',
    '*.gtimg.cn',
    '*.10jqka.com.cn',
  ];
  const inherited = process.env.NO_PROXY || process.env.no_proxy || '';
  const merged = Array.from(new Set([
    ...inherited.split(',').map((x) => x.trim()).filter(Boolean),
    ...marketHosts,
  ])).join(',');

  return {
    ...process.env,
    NO_PROXY: merged,
    no_proxy: merged,
    FINRESEARCH_MARKET_PROXY_BYPASS: process.env.FINRESEARCH_MARKET_PROXY_BYPASS || '1',
  };
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 600,
    title: '景渊 · 金融研究助手',
    frame: false,            // 无原生边框：去掉 Windows 标题栏按钮，仅保留应用内 TitleBar
    autoHideMenuBar: true,
    icon: windowIcon(),      // 窗口/任务栏图标（统一品牌资产）
    backgroundColor: '#0b1220',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadURL(`http://127.0.0.1:${port}/`);
  win.on('closed', () => { win = null; });

  // 把最大化/还原状态变化推给渲染进程，TitleBar 据此切换图标
  const pushMax = () => win.webContents.send('finresearch:window:maximize-change', win.isMaximized());
  win.on('maximize', pushMax);
  win.on('unmaximize', pushMax);
}

function registerWindowIpc() {
  // 应用内 TitleBar 通过这些通道控制窗口（preload 转发）
  ipcMain.on('finresearch:window:minimize', () => win && win.minimize());
  ipcMain.on('finresearch:window:maximize-toggle', () => {
    if (!win) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });
  ipcMain.on('finresearch:window:close', () => win && win.close());
  ipcMain.handle('finresearch:window:is-maximized', () => !!(win && win.isMaximized()));
}

async function start() {
  const port = await launchBackend();
  registerWindowIpc();
  createWindow(port);
}

async function launchBackend() {
  const exe = backendExe();
  clearStalePort();
  appendBackendLog(`launch ${exe}`);
  const child = spawn(exe, [], { cwd: path.dirname(exe), env: backendEnv(), windowsHide: true });
  backend = child;
  const launchFailed = new Promise((_, reject) => {
    child.once('error', (e) => reject(e));
    child.once('exit', (code, signal) => reject(new Error(`backend exited before ready code=${code} signal=${signal || ''}`)));
  });
  child.stdout.on('data', (chunk) => logBackendChunk('[stdout]', chunk));
  child.stderr.on('data', (chunk) => logBackendChunk('[stderr]', chunk));
  child.on('error', (e) => {
    appendBackendLog(`[error] ${e && e.stack ? e.stack : e}`);
  });
  child.on('exit', (code, signal) => {
    appendBackendLog(`[exit] code=${code} signal=${signal || ''}`);
    if (backend === child) backend = null;
    if (!quitting) restartBackend();
  });

  const port = await Promise.race([
    (async () => {
      const p = await readPort();
      await waitHealth(p);
      return p;
    })(),
    launchFailed,
  ]);
  backendRestartAttempts = 0;
  appendBackendLog(`ready http://127.0.0.1:${port}/`);
  return port;
}

async function restartBackend() {
  if (restartingBackend || quitting) return;
  restartingBackend = true;
  try {
    while (!quitting && backendRestartAttempts < 3) {
      backendRestartAttempts += 1;
      await delay(1000 * backendRestartAttempts);
      try {
        appendBackendLog(`[restart] attempt=${backendRestartAttempts}`);
        const port = await launchBackend();
        if (win && !win.isDestroyed()) {
          win.loadURL(`http://127.0.0.1:${port}/`);
        }
        return;
      } catch (e) {
        appendBackendLog(`[restart-failed] ${e && e.stack ? e.stack : e}`);
        if (backend) {
          try { backend.kill(); } catch (_) {}
          backend = null;
        }
      }
    }
  } finally {
    restartingBackend = false;
  }
}

// Windows 任务栏分组：在创建窗口前设置 AppUserModelId，配合 exe 图标正确分组。
// 必须与 electron-builder.yml 的 appId 一致，否则任务栏图标会与快捷方式分成两组。
app.setAppUserModelId('com.jingyuan.app');

app.whenReady().then(start).catch((e) => {
  appendBackendLog(`[startup-failed] ${e && e.stack ? e.stack : e}`);
  console.error('启动失败：', e);
  app.quit();
});
app.on('window-all-closed', () => {
  quitting = true;
  if (backend) { try { backend.kill(); } catch (_) {} }
  app.quit();
});
app.on('before-quit', () => {
  quitting = true;
  if (backend) { try { backend.kill(); } catch (_) {} }
});
