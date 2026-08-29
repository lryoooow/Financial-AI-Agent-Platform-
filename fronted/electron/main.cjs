// Electron 主进程：拉起 Python 后端 sidecar，开无原生边框窗口加载其本地 URL。
// frame:false 去掉 Windows 原生标题栏按钮，窗口控制由 preload 暴露的 IPC 由应用内 TitleBar 触发。
// 图标统一：窗口/任务栏用 electron/icon.ico（随 electron/ 一并打包）。
//
// v1.6.0（审计修复，对应问题编号见各行注释）：
// - [B2] 单实例锁：双开曾导致 port.txt 互删/双后端互踩/孤儿进程（生产日志 7/16、7/18 两次实锤）。
// - [B2] 启动 splash：后端冷启动 3-11s 期间此前无任何窗口（最长 ~4 分钟黑等），现在立即出 splash。
// - [B2] waitHealth 请求超时：后端僵死（端口可连但不响应）时启动不再无限悬挂。
// - [B2] 重启 3 次失败不再静默留死页：弹错误框并退出。
// - [B3] 优雅退出：先 POST /api/shutdown 握手等后端自行收尾（lifespan/检查点 flush），
//        最多 5s，超时才 kill（Windows kill=TerminateProcess，此前 lifespan 收尾 0 次执行过）。
// - [B6] NO_PROXY 通配符修正：`*.eastmoney.com` 对 Python 的 endswith 匹配永远无效，
//        行情域名清单由后端 market_provider 单一权威维护，本进程不再注入自己的清单。
// - [D3] backend.log 只记生命周期事件（stdout/stderr 详细日志由 Python 侧 sidecar 日志承载，
//        此前同一输出双份采集）；超 5MB 截断，不再无限追加。
// - [D7] 加固：setWindowOpenHandler 全拒 + will-navigate 白名单 + IPC 校验 sender。

const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');

let backend = null;
let win = null;
let splash = null;
let backendPort = null;
let quitting = false;
let restartingBackend = false;
let backendRestartAttempts = 0;

// [B2] 单实例锁：第二个实例只聚焦既有窗口，不再拉起第二套后端
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
}

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

const BACKEND_LOG_MAX = 5 * 1024 * 1024;

function appendBackendLog(message) {
  try {
    fs.mkdirSync(appDataDir(), { recursive: true });
    const file = backendLogFile();
    // [D3] 简单截断：超 5MB 只留最后 1MB，防长期使用无限增长
    try {
      const st = fs.statSync(file);
      if (st.size > BACKEND_LOG_MAX) {
        const tail = fs.readFileSync(file, 'utf8').slice(-1024 * 1024);
        fs.writeFileSync(file, tail);
      }
    } catch (_) {}
    fs.appendFileSync(file, `${new Date().toISOString()} ${message}\n`, 'utf8');
  } catch (_) {}
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function portFile() {
  return path.join(appDataDir(), 'port.txt');
}

function shutdownTokenFile() {
  return path.join(appDataDir(), 'shutdown-token.txt');
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
      // [B2] 单次探测 3s 超时：后端僵死（可连不响应）时 retry 才有机会触发
      req.setTimeout(3000, () => req.destroy(new Error('health probe timeout')));
      req.on('error', retry);
      function retry() {
        if (++n > retries) return reject(new Error('后端健康检查超时'));
        setTimeout(tick, 500);
      }
    };
    tick();
  });
}

// [B6] 行情 NO_PROXY 域名清单由后端 market_provider.py 单一权威维护（Python 端
// endswith 匹配吃不下 `*.host` 通配符）；本进程只透传环境与开关。
function backendEnv() {
  return {
    ...process.env,
    FINRESEARCH_MARKET_PROXY_BYPASS: process.env.FINRESEARCH_MARKET_PROXY_BYPASS || '1',
  };
}

// [B2] 启动 splash：后端 ready 之前先有个反馈窗口（内联 ASCII HTML，无外部依赖）
function createSplash() {
  splash = new BrowserWindow({
    width: 460,
    height: 300,
    frame: false,
    resizable: false,
    center: true,
    backgroundColor: '#0b1220',
    icon: windowIcon(),
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  const html =
    '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' +
    'body{margin:0;background:#0b1220;color:#e5e7eb;font-family:"Microsoft YaHei",system-ui,sans-serif;' +
    'display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:18px}' +
    '.ring{width:44px;height:44px;border-radius:50%;border:3px solid #263042;border-top-color:#d97757;' +
    'animation:spin 0.9s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}' +
    '.t{font-size:15px;letter-spacing:2px}.s{font-size:11px;color:#6b7280}' +
    '</style></head><body><div class="ring"></div><div class="t">景渊 · 正在启动研究引擎</div>' +
    '<div class="s">首次启动需加载数据组件，请稍候…</div></body></html>';
  splash.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  splash.on('closed', () => { splash = null; });
}

function closeSplash() {
  if (splash && !splash.isDestroyed()) splash.close();
  splash = null;
}

function createWindow(port) {
  backendPort = port;
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
    show: false,             // [B2] 先隐藏，ready-to-show 再显示并关 splash（无白屏闪）
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadURL(`http://127.0.0.1:${port}/`);
  win.once('ready-to-show', () => {
    win.show();
    closeSplash();
  });
  win.on('closed', () => { win = null; });

  // [D7] 加固：渲染内容来自本地后端，不允许开新窗口与外部跳转。
  // 核验修复：用 URL 解析精确比对 host —— 前缀匹配可被 `http://127.0.0.1:9527@evil/`
  // 这类 userinfo 形态绕过。
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => {
    try {
      const u = new URL(url);
      if (u.host !== `127.0.0.1:${port}` && u.host !== `localhost:${port}`) event.preventDefault();
    } catch (_) {
      event.preventDefault();
    }
  });

  // 把最大化/还原状态变化推给渲染进程，TitleBar 据此切换图标
  const pushMax = () => win.webContents.send('finresearch:window:maximize-change', win.isMaximized());
  win.on('maximize', pushMax);
  win.on('unmaximize', pushMax);
}

function registerWindowIpc() {
  // 应用内 TitleBar 通过这些通道控制窗口（preload 转发）。
  // [D7] 校验 sender：只接受主窗口 webContents 的调用。
  const isMainWindow = (event) => win && !win.isDestroyed() && event.sender === win.webContents;
  ipcMain.on('finresearch:window:minimize', (e) => { if (isMainWindow(e)) win.minimize(); });
  ipcMain.on('finresearch:window:maximize-toggle', (e) => {
    if (!isMainWindow(e)) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });
  ipcMain.on('finresearch:window:close', (e) => { if (isMainWindow(e)) win.close(); });
  ipcMain.handle('finresearch:window:is-maximized', (e) => isMainWindow(e) && !!win.isMaximized());
}

// [B3] 优雅退出：先握手让后端自己走 lifespan 收尾（检查点 flush、日志「关闭。」），
// 等它退出最多 5s，超时才硬杀。
function requestBackendShutdown() {
  // 核验修复：附启动时随机生成的 X-Shutdown-Token（自定义头强制 CORS 预检，
  // 跨站 no-cors 表单无法伪造本握手关掉本地后端）。
  return new Promise((resolve) => {
    if (!backend || backendPort == null) return resolve(false);
    let token = '';
    try { token = fs.readFileSync(shutdownTokenFile(), 'utf8').trim(); } catch (_) {}
    const req = http.request(
      {
        host: '127.0.0.1', port: backendPort, path: '/api/shutdown', method: 'POST', timeout: 2000,
        headers: token ? { 'X-Shutdown-Token': token } : {},
      },
      (res) => { res.resume(); resolve(true); },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.end();
  });
}

async function stopBackendGracefully() {
  if (!backend) return;
  const child = backend;
  await requestBackendShutdown();
  await new Promise((resolve) => {
    child.once('exit', () => resolve(true));
    setTimeout(resolve, 5000); // 最多等 5s
  });
  try { child.kill(); } catch (_) {} // 仍在就硬杀（TerminateProcess）
  if (backend === child) backend = null;
}

async function start() {
  createSplash();          // [B2] 立即出反馈窗口
  registerWindowIpc();
  try {
    const port = await launchBackend();
    createWindow(port);
  } catch (e) {
    appendBackendLog(`[startup-failed] ${e && e.stack ? e.stack : e}`);
    closeSplash();
    dialog.showErrorBox(
      '景渊启动失败',
      `本地研究引擎启动失败：${e && e.message ? e.message : e}\n\n` +
      '日志位置：%APPDATA%\\FinResearch\\backend.log 与 backend-sidecar.log',
    );
    app.quit();
  }
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
  // [D3] stdout/stderr 不再镜像进 backend.log（Python 侧自己写 backend-sidecar.log，
  // 此前同一输出双份采集）；只保留进程错误记录。
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
    // [B2] 3 次耗尽：明确告知用户并退出，不再静默留一个死页窗口
    if (!quitting) {
      appendBackendLog('[restart-gave-up] 3 attempts exhausted');
      dialog.showErrorBox(
        '景渊 · 后端反复重启失败',
        '本地研究引擎连续 3 次启动失败，应用将退出。\n' +
        '请查看 %APPDATA%\\FinResearch\\backend.log 排查（端口占用/杀软拦截/文件缺失是常见原因）。',
      );
      app.quit();
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
  closeSplash();
  console.error('启动失败：', e);
  app.quit();
});

async function quitAll() {
  quitting = true;
  await stopBackendGracefully(); // [B3]
  app.quit();
}

app.on('window-all-closed', () => { quitAll(); });
app.on('before-quit', (e) => {
  if (quitting) return;
  e.preventDefault();       // 等优雅退出完成后再真正退出
  quitAll();
});
