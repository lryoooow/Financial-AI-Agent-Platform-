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
//
// v1.8.0：后台驻留 —— 关闭窗口 = 隐藏到系统托盘（研究任务在后台继续），只有托盘菜单
// 「退出」才完全退出（后端优雅停机）。偏好（托盘驻留 / 开机自启）存
// %APPDATA%/FinResearch/desktop-settings.json，由设置页经 IPC 读写；开机自启以 --hidden
// 参数启动，直接驻留托盘不弹窗口。

const { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, screen, session, shell, Tray } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const isWin = process.platform === 'win32';
const isMac = process.platform === 'darwin';

let backend = null;
let win = null;
let splash = null;
let backendPort = null;
let quitting = false;
let restartingBackend = false;
let backendRestartAttempts = 0;
let tray = null;
let trayHintShown = false;
// Windows 注销/关机期间：关闭窗口不拦截（与「正在退出」分开记 —— 关机被取消后会话继续，
// 若复用 quitting，quitAll 的重入守卫会让之后再也无法退出、后端成孤儿进程）
let sessionEnding = false;
// 隐藏启动（开机自启）期间用户又双击了快捷方式：窗口就绪后要显示
let showWhenReady = false;
// 开机自启以 --hidden 启动：不出 splash、不弹窗口，直接驻留托盘
const startHidden = process.argv.includes('--hidden');

// [B2] 单实例锁：第二个实例只聚焦既有窗口，不再拉起第二套后端
const explicitProfile = configuredDataDir();
if (app.isPackaged && explicitProfile) {
  const profile = path.join(explicitProfile, 'electron');
  fs.mkdirSync(profile, { recursive: true });
  app.setPath('userData', profile);
}
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showMainWindow();
  });
}

// 应用内窗口图标：electron/icon.ico 在 dev 与 packaged 下都随 main.cjs 同目录存在。
function windowIcon() {
  return path.join(__dirname, isWin ? 'icon.ico' : 'icons/icon.png');
}

function backendExe() {
  const executable = isWin ? 'FinResearch.exe' : 'FinResearch';
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend', executable);
  }
  // dev：指向已打包的后端 exe（先在 backend 跑过 pyinstaller）
  return path.join(__dirname, '..', '..', 'backend', 'dist', 'FinResearch', executable);
}

// 产品名 1.2.2 起改为「景渊 JingYuan」，但用户数据目录仍沿用 FinResearch：
// 这里存着 SQLite（对话、报告）、模型密钥与 port.txt。改名会让老用户的数据凭空消失，
// 且必须与 backend/config.py 的 app_data_dir() 保持一致（两端靠 port.txt 握手）。
// v1.6.3（D5）：APPDATA 环境变量优先（与后端 config.py 同算法——后端读
// os.environ["APPDATA"]，旧实现硬拼 homedir，APPDATA 被重定向时两端目录分叉、握手必失败）。
function configuredDataDir() {
  const explicit = process.env.FINRESEARCH_DATA_DIR?.trim();
  if (explicit) return path.resolve(explicit);
  if (app.isPackaged) {
    try {
      const directory = process.env.PORTABLE_EXECUTABLE_DIR
        || (isMac ? path.resolve(path.dirname(process.execPath), '../../..') : path.dirname(process.execPath));
      const portable = path.join(directory, 'portable-data');
      if (process.env.PORTABLE_EXECUTABLE_DIR || fs.existsSync(path.join(directory, '.portable')) || fs.existsSync(portable)) {
        return portable;
      }
    } catch (_) {}
  }
  return null;
}

function appDataDir() {
  const configured = configuredDataDir();
  if (configured) return configured;
  const home = os.homedir();
  if (isWin) return path.join(process.env.APPDATA || path.join(home, 'AppData', 'Roaming'), 'FinResearch');
  if (isMac) return path.join(home, 'Library', 'Application Support', 'FinResearch');
  return path.join(process.env.XDG_DATA_HOME || path.join(home, '.local', 'share'), 'FinResearch');
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

// ── v1.8.0：桌面偏好（托盘驻留 / 开机自启） ─────────────────
const DEFAULT_DESKTOP_PREFS = { closeToTray: true, autoLaunch: false };

function desktopPrefsFile() {
  return path.join(appDataDir(), 'desktop-settings.json');
}

function readDesktopPrefs() {
  try {
    const raw = JSON.parse(fs.readFileSync(desktopPrefsFile(), 'utf8'));
    return {
      closeToTray: typeof raw.closeToTray === 'boolean' ? raw.closeToTray : DEFAULT_DESKTOP_PREFS.closeToTray,
      autoLaunch: typeof raw.autoLaunch === 'boolean' ? raw.autoLaunch : DEFAULT_DESKTOP_PREFS.autoLaunch,
    };
  } catch (_) {
    return { ...DEFAULT_DESKTOP_PREFS };
  }
}

let desktopPrefs = readDesktopPrefs();

function writeDesktopPrefs(next) {
  desktopPrefs = { ...desktopPrefs, ...next };
  try {
    fs.mkdirSync(appDataDir(), { recursive: true });
    fs.writeFileSync(desktopPrefsFile(), JSON.stringify(desktopPrefs, null, 2), 'utf8');
  } catch (e) {
    appendBackendLog(`[prefs-write-failed] ${e && e.message ? e.message : e}`);
  }
  if (Object.hasOwn(next, 'autoLaunch')) applyAutoLaunch();
  return desktopPrefs;
}

function applyAutoLaunch() {
  if (!app.isPackaged) return; // 开发态不写注册表
  try {
    // 便携版运行时 execPath 指向解压临时目录，自启必须指向便携 exe 本体
    const exePath = process.env.PORTABLE_EXECUTABLE_FILE || process.execPath;
    const launch = { openAtLogin: !!desktopPrefs.autoLaunch, args: ['--hidden'] };
    const profile = configuredDataDir();
    if (isWin && profile) {
      const hash = require('node:crypto').createHash('sha256').update(profile.toLowerCase()).digest('hex').slice(0, 12);
      app.setLoginItemSettings({ ...launch, path: exePath, name: 'JingYuan-' + hash });
    } else {
      app.setLoginItemSettings(isWin ? { ...launch, path: exePath } : launch);
    }
  } catch (e) {
    appendBackendLog(`[auto-launch-failed] ${e && e.message ? e.message : e}`);
  }
}

function ensureWindowVisibleOnScreen(targetWin) {
  try {
    const b = targetWin.getBounds();
    const displays = screen.getAllDisplays();
    const intersects = displays.some((d) => {
      const wa = d.workArea;
      const overlapX = Math.min(b.x + b.width, wa.x + wa.width) - Math.max(b.x, wa.x);
      const overlapY = Math.min(b.y + b.height, wa.y + wa.height) - Math.max(b.y, wa.y);
      return overlapX >= 120 && overlapY >= 80;
    });
    if (!intersects) targetWin.center();
  } catch (_) {}
}

function showMainWindow() {
  if (!win || win.isDestroyed()) {
    showWhenReady = true; // 窗口尚未创建（后端仍在启动）：就绪后显示
    return;
  }
  if (win.isMinimized()) win.restore();
  ensureWindowVisibleOnScreen(win);
  if (!win.isVisible()) win.show();
  win.focus();
}

function createTray() {
  if (tray) return;
  tray = new Tray(isMac ? path.join(__dirname, 'icons/tray.png') : windowIcon());
  tray.setToolTip('景渊 · 金融研究助手');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开景渊', click: () => showMainWindow() },
    { type: 'separator' },
    { label: '退出', click: () => { quitAll(); } },
  ]));
  tray.on('click', () => { sessionEnding = false; showMainWindow(); });
  tray.on('double-click', () => showMainWindow());
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
    let settled = false;
    const tick = () => {
      if (settled) return;
      const req = http.get(`http://127.0.0.1:${port}/api/health`, (res) => {
        let raw = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          if (raw.length < 4096) raw += chunk;
        });
        res.on('end', () => {
          if (settled) return;
          if (res.statusCode === 200) {
            try {
              const parsed = JSON.parse(raw);
              if (parsed && parsed.status === 'ok') {
                settled = true;
                resolve();
                return;
              }
            } catch (_) {}
          }
          retry();
        });
      });
      // [B2] 单次探测 3s 超时：后端僵死（可连不响应）时 retry 才有机会触发
      req.setTimeout(3000, () => req.destroy(new Error('health probe timeout')));
      req.on('error', retry);
      function retry() {
        if (settled) return;
        if (++n > retries) {
          settled = true;
          return reject(new Error('后端健康检查超时'));
        }
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
    FINRESEARCH_DATA_DIR: appDataDir(),
    FIN_WEB: '0',
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
  let initWidth = 1280;
  let initHeight = 820;
  let minWidth = 960;
  let minHeight = 600;
  try {
    const wa = screen.getPrimaryDisplay().workAreaSize;
    if (wa && wa.width && wa.height) {
      initWidth = Math.min(1280, Math.max(800, wa.width - 24));
      initHeight = Math.min(820, Math.max(560, wa.height - 24));
      minWidth = Math.min(960, Math.max(760, wa.width - 40));
      minHeight = Math.min(600, Math.max(520, wa.height - 40));
    }
  } catch (_) {}
  win = new BrowserWindow({
    width: initWidth,
    height: initHeight,
    minWidth,
    minHeight,
    title: '景渊 · 金融研究助手',
    ...(isMac ? { titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 12, y: 10 } } : { frame: false }),
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
    if (!startHidden || showWhenReady) win.show();
    closeSplash();
  });
  // Windows 注销/关机：不拦截关闭（否则托盘驻留会阻止会话结束）
  win.on('query-session-end', () => { sessionEnding = true; });
  win.on('session-end', () => { quitAll(); });
  // v1.8.0：关闭窗口 = 隐藏到托盘（研究任务后台继续）；只有托盘「退出」或关闭了托盘驻留才真正退出
  win.on('close', (e) => {
    if (quitting || sessionEnding || !desktopPrefs.closeToTray) return;
    e.preventDefault();
    win.hide();
    if (!trayHintShown && tray && process.platform === 'win32') {
      trayHintShown = true;
      try {
        tray.displayBalloon({
          iconType: 'info',
          title: '景渊仍在后台运行',
          content: '研究任务会继续进行。点击托盘图标恢复窗口，右键选择「退出」完全退出。',
        });
      } catch (_) {}
    }
  });
  // v1.6.3（D5）：加载失败兜底 —— 后端在 health 通过后、窗口加载前死掉时
  // ready-to-show 永不触发，splash 会永久卡死。失败即关 splash + 弹错（restart 循环自会接手）。
  win.webContents.once('did-fail-load', (_e, code, desc, url, isMain) => {
    if (!isMain) return;
    appendBackendLog(`[did-fail-load] code=${code} desc=${desc}`);
    closeSplash();
  });
  win.on('closed', () => { win = null; });

  // [D7] 加固：渲染内容来自本地后端，禁止在应用内打开新窗口；合法外链交给系统浏览器打开。
  // 核验修复：用 URL 解析精确比对 host，并使用动态 backendPort（兼容后端异常重启换端口）。
  win.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const u = new URL(url);
      if (
        (u.protocol === 'https:' || u.protocol === 'http:') &&
        u.hostname !== '127.0.0.1' &&
        u.hostname !== 'localhost'
      ) {
        shell.openExternal(url).catch(() => {});
      }
    } catch (_) {}
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    try {
      const u = new URL(url);
      const activePort = backendPort || port;
      if (u.host !== `127.0.0.1:${activePort}` && u.host !== `localhost:${activePort}`) {
        event.preventDefault();
        if (
          (u.protocol === 'https:' || u.protocol === 'http:') &&
          u.hostname !== '127.0.0.1' &&
          u.hostname !== 'localhost'
        ) {
          shell.openExternal(url).catch(() => {});
        }
        return;
      }
      if (u.pathname.startsWith('/api/')) {
        event.preventDefault();
      }
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
  // v1.8.0：桌面偏好读写（设置页）
  const prefsPayload = () => ({ ...desktopPrefs, version: app.getVersion() });
  ipcMain.handle('finresearch:app:get-prefs', (e) => (isMainWindow(e) ? prefsPayload() : null));
  ipcMain.handle('finresearch:app:set-prefs', (e, patch) => {
    if (!isMainWindow(e) || !patch || typeof patch !== 'object') return prefsPayload();
    const next = {};
    if (typeof patch.closeToTray === 'boolean') next.closeToTray = patch.closeToTray;
    if (typeof patch.autoLaunch === 'boolean') next.autoLaunch = patch.autoLaunch;
    writeDesktopPrefs(next);
    return prefsPayload();
  });
  ipcMain.handle('finresearch:clipboard:write-image', (e, dataUrl) => {
    if (!isMainWindow(e) || typeof dataUrl !== 'string' || !dataUrl.startsWith('data:image/')) return false;
    try {
      const img = nativeImage.createFromDataURL(dataUrl);
      if (img.isEmpty()) return false;
      clipboard.writeImage(img);
      return true;
    } catch (_) {
      return false;
    }
  });
}

// 先通知后端取消超时的请求并刷新检查点，再等待进程退出。
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
      (res) => { res.resume(); resolve(res.statusCode === 200); },
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
  if (child.exitCode === null && child.signalCode === null) {
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.removeListener('exit', onExit);
        try { child.kill(); } catch (_) {}
        resolve();
      }, 10000);
      function onExit() {
        clearTimeout(timer);
        resolve();
      }
      child.once('exit', onExit);
    });
  }
  if (backend === child) backend = null;
}

let backendStableTimer = null;

async function start() {
  if (!startHidden) createSplash();          // [B2] 立即出反馈窗口（开机自启隐藏启动时不出）
  registerWindowIpc();
  createTray();
  try {
    const port = await launchBackend();
    // v1.8.0：界面由本地后端伺服，HTTP 缓存毫无收益却会在升级后继续显示旧版界面
    //（1.7→1.8 实测：安装新版后窗口仍是旧 index.html + 旧脚本）。每次启动清掉 HTTP 缓存，
    // 只清缓存，不动 localStorage（用户的界面偏好）。
    try { await session.defaultSession.clearCache(); } catch (e) { appendBackendLog(`[clear-cache-failed] ${e}`); }
    createWindow(port);
  } catch (e) {
    if (quitting) return;
    appendBackendLog(`[startup-failed] ${e && e.stack ? e.stack : e}`);
    closeSplash();
    dialog.showErrorBox(
      '景渊启动失败',
      `本地研究引擎启动失败：${e && e.message ? e.message : e}\n\n` +
      `日志位置：${backendLogFile()} 与 backend-sidecar.log`,
    );
    app.quit();
  }
}

async function launchBackend() {
  if (quitting) throw new Error('app is quitting');
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
    if (backendStableTimer) {
      clearTimeout(backendStableTimer);
      backendStableTimer = null;
    }
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
  if (backendStableTimer) clearTimeout(backendStableTimer);
  backendStableTimer = setTimeout(() => {
    if (backend === child && !quitting) backendRestartAttempts = 0;
    backendStableTimer = null;
  }, 30000);
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
        // v1.6.3（D5）：同步 backendPort —— 退出握手 POST 用它；旧实现不更新，
        // 重启换端口后握手打到旧端口，优雅停机静默退化成硬杀。
        backendPort = port;
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
        `请查看 ${backendLogFile()} 排查（端口占用/杀软拦截/文件缺失是常见原因）。`,
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

// v1.8.0（冒烟实测）：未拿到单实例锁的进程只负责把消息转给已运行实例然后退出，
// 绝不能再走 start() —— 旧实现 app.quit() 是异步的，whenReady 仍会触发：第二个实例
// 拉起第二套后端并删掉运行中实例的 port.txt。托盘驻留后用户常在后台运行时再点快捷方式，必须挡住。
if (gotLock) app.whenReady().then(start).catch((e) => {
  appendBackendLog(`[startup-failed] ${e && e.stack ? e.stack : e}`);
  closeSplash();
  console.error('启动失败：', e);
  app.quit();
});

async function quitAll() {
  if (quitting) return;
  quitting = true;
  await stopBackendGracefully(); // [B3]
  if (tray) {
    try { tray.destroy(); } catch (_) {}
    tray = null;
  }
  app.quit();
}

// 托盘驻留时窗口只是隐藏，不会触发本事件；关闭了托盘驻留（或窗口真被销毁）时才退出
app.on('activate', () => { showMainWindow(); });
app.on('window-all-closed', () => { quitAll(); });
app.on('before-quit', (e) => {
  if (quitting) return;
  e.preventDefault();       // 等优雅退出完成后再真正退出
  quitAll();
});
