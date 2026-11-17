# 景渊 JingYuan · 前端与桌面端工作台

本目录包含景渊（JingYuan `v0.1.1`）的前端 Web 界面（React 18 + TypeScript + Vite 6 + Tailwind CSS v4）与 Electron 桌面端外壳。

## 常用开发命令

在仓库根目录执行：

```bash
# 安装锁定依赖
pnpm --dir fronted install --frozen-lockfile

# 启动前端热更新开发服（默认监听 http://127.0.0.1:5173）
VITE_API_BASE=http://127.0.0.1:8000 pnpm --dir fronted dev

# 运行严格类型检查与生产构建
pnpm --dir fronted typecheck
pnpm --dir fronted build
```

完整自部署 Web 启动与桌面端打包说明请参见根目录 [README.md](../README.md)。
