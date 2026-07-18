// 预加载：在 contextIsolation 下向渲染进程暴露最小、显式的窗口控制 API。
// 应用内 TitleBar 通过 window.finresearch.window.* 控制窗口（根因 2）。
// 仅暴露窗口控制 + 最大化状态订阅，不暴露 Node 能力，保持安全边界。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('finresearch', {
  window: {
    minimize: () => ipcRenderer.send('finresearch:window:minimize'),
    maximizeToggle: () => ipcRenderer.send('finresearch:window:maximize-toggle'),
    close: () => ipcRenderer.send('finresearch:window:close'),
    isMaximized: () => ipcRenderer.invoke('finresearch:window:is-maximized'),  // Promise<boolean>
    // 订阅最大化状态变化（主进程 push）；返回取消订阅函数。
    onMaximizeChange: (cb) => {
      if (typeof cb !== 'function') return () => {};
      const handler = (_e, isMaximized) => cb(!!isMaximized);
      ipcRenderer.on('finresearch:window:maximize-change', handler);
      return () => ipcRenderer.removeListener('finresearch:window:maximize-change', handler);
    },
  },
});
