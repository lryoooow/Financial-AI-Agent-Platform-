# 景渊本地数据布局（用户电脑上的落点）

所有用户数据只存在本机 `%APPDATA%\FinResearch\`（即 `C:\Users\<用户>\AppData\Roaming\FinResearch\`），
不上传、不联网同步。卸载重装不丢数据；换机迁移 = 整目录拷贝。

```
%APPDATA%\FinResearch\
├── finresearch.db            # 结构化主库（SQLite）
│     · chat_conversation     会话元数据（标题/模型/归档时间）
│     · chat_delta            会话行级消息（活跃期全量保存）
│     · research_task         研究任务与阶段事件
│     · report                研究报告全文（html/payload/摘要）
│     · local_setting         用户画像、会话归档摘要、诊断等键值
│     · (langgraph 检查点表)   对话/研究线程的中间状态
│
├── rag.db                    # 向量库 + 知识库（sqlite-vec 扩展，独立文件）
│     · chunks                知识块：来源(chat/report/summary)/引用/标题/正文/时间/哈希去重
│     · chunks_vec            vec0 虚拟表：256 维嵌入（本地确定性嵌入器）
│
├── kb\
│   └── {文档id}.{ext}         # 知识库原始文档副本（用户上传的 txt/md/pdf/docx）
│
├── archive\
│   └── conversations\
│       └── {会话id}.jsonl.gz  # 超期会话原文的压缩归档（生命周期清理后本地仍可溯源）
│
├── context\
│   └── conversations\{id}\media\   # 会话图片等媒体卸载
│
├── assets\
│   └── images\               # 图片资产（内容哈希命名，服务端已降采样）
│
├── port.txt                  # 后端端口握手文件（Electron 读取）
└── backend.log               # 后端运行日志
```

## 记忆与知识库的生命周期

| 数据 | 活跃期 | 超期后 |
|---|---|---|
| 会话消息（chat_delta） | 全量保存 | 闲置 90 天 → LLM 压缩成 ≤300 字摘要（存 local_setting + 向量库），原文压缩进 `archive/`，行级消息删除；继续该会话时以摘要恢复上下文 |
| 对话片段向量（chat） | 可被检索 | 180 天后从 rag.db 清理（摘要块保留） |
| 研究报告（report + 向量块） | 永久 | 永久（用户成果资产，`search_memory` 可回忆） |
| 会话摘要（summary） | 永久 | 永久 |

检索分两级：`search_memory`（索引层）返回带 ref 引用号的索引卡（≤300 字摘要片段）；
`read_memory`（原文层）按 ref 深读完整原文——报告全文（主库）、归档对话原文（gzip 解压）、
知识库文档全文。用户知识库（kb/ + kb_documents 表）支持上传 txt/md/pdf/docx，
对话与检索自动覆盖；索引与摘要即 rag.db 结构化列（无独立内存缓存，SQLite 页缓存自动调温）。

## 设计原则

- **本地优先**：嵌入用本地确定性嵌入器（零依赖、离线可用），不向任何服务发送用户历史。
- **优雅降级**：sqlite-vec 扩展缺失/损坏时记忆功能自动停用，主链路（对话/研究/行情）不受影响。
- **清理不销毁**：生命周期清理的所有内容在 `archive/` 有压缩副本，本地永远可溯源。
