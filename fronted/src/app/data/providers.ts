// 预置 LLM 厂商（OpenAI 兼容端点）+ 各厂商 2026-07 当前模型清单。
// 用户填 API Key → 测试连通性 → "拉取最新模型"会真·调厂商 /v1/models 实拉（见 api.models），
// 不再依赖这里的静态清单。静态清单仅作默认/离线兜底。
// 数字/推理全部走这里配置的云端端点（规划文档：唯一出网点）。

export type Region = "domestic" | "foreign";

export interface ModelInfo {
  id: string;
  label: string;
  context: string; // 上下文窗口
  tag?: string; // 旗舰/推荐/极速/推理/长文/代码/视觉/免费/稳定 等
}

export interface Provider {
  id: string;
  name: string;
  vendor: string;
  region: Region;
  baseUrl: string;
  docsHint: string;
  accent: string; // 品牌色
  short: string; // 图标字母
  // 默认模型清单（2026-07 当前线）；"拉取最新模型"会从厂商实时覆盖
  models: ModelInfo[];
}

export const PROVIDERS: Provider[] = [
  // ───────────── 国内 ─────────────
  {
    id: "deepseek",
    name: "DeepSeek",
    vendor: "深度求索",
    region: "domestic",
    baseUrl: "https://api.deepseek.com",
    docsHint: "platform.deepseek.com 获取 API Key（当前 V4 线）",
    accent: "#4d6bfe",
    short: "DS",
    models: [
      { id: "deepseek-v4-pro", label: "DeepSeek-V4-Pro", context: "1M", tag: "旗舰" },
      { id: "deepseek-v4-flash", label: "DeepSeek-V4-Flash", context: "1M", tag: "极速" },
    ],
  },
  {
    id: "qwen",
    name: "通义千问",
    vendor: "阿里云百炼",
    region: "domestic",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    docsHint: "阿里云百炼控制台创建 DashScope Key",
    accent: "#615ced",
    short: "QW",
    models: [
      { id: "qwen3-max", label: "Qwen3-Max", context: "256K", tag: "旗舰" },
      { id: "qwen-plus", label: "Qwen3-Plus", context: "1M", tag: "推荐" },
      { id: "qwen-flash", label: "Qwen3-Flash", context: "1M", tag: "极速" },
      { id: "qwq-plus", label: "QwQ-Plus 推理", context: "128K", tag: "推理" },
      { id: "qwen-long", label: "Qwen-Long", context: "10M", tag: "长文" },
    ],
  },
  {
    id: "zhipu",
    name: "智谱 GLM",
    vendor: "智谱 AI",
    region: "domestic",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    docsHint: "bigmodel.cn 开放平台获取 Key",
    accent: "#3859ff",
    short: "GLM",
    models: [
      { id: "glm-5.2", label: "GLM-5.2", context: "1M", tag: "旗舰" },
      { id: "glm-5", label: "GLM-5", context: "200K", tag: "推荐" },
      { id: "glm-5-turbo", label: "GLM-5-Turbo", context: "200K", tag: "极速" },
      { id: "glm-4.5-flash", label: "GLM-4.5-Flash", context: "128K", tag: "免费" },
    ],
  },
  {
    id: "moonshot",
    name: "月之暗面 Kimi",
    vendor: "Moonshot AI",
    region: "domestic",
    baseUrl: "https://api.moonshot.cn/v1",
    docsHint: "platform.kimi.com 获取 Key",
    accent: "#16182d",
    short: "KI",
    models: [
      { id: "kimi-k2.7-code", label: "Kimi K2.7 Code", context: "256K", tag: "旗舰" },
      { id: "kimi-k2.7-code-highspeed", label: "Kimi K2.7 高速版", context: "256K", tag: "极速" },
      { id: "kimi-k2.6", label: "Kimi K2.6", context: "256K", tag: "推荐" },
    ],
  },
  {
    id: "doubao",
    name: "豆包",
    vendor: "字节火山方舟",
    region: "domestic",
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    docsHint: "火山方舟控制台创建推理接入点（模型 id 即接入点对应模型）",
    accent: "#3b5bff",
    short: "DB",
    models: [
      { id: "doubao-seed-2-0-pro-260215", label: "Doubao Seed 2.0 Pro", context: "256K", tag: "旗舰" },
      { id: "doubao-seed-2-0-mini-260428", label: "Doubao Seed 2.0 Mini", context: "256K", tag: "推荐" },
      { id: "doubao-seed-2-0-lite-260428", label: "Doubao Seed 2.0 Lite", context: "256K", tag: "极速" },
      { id: "doubao-seed-2-0-code-preview-260215", label: "Doubao Seed 2.0 Code", context: "256K", tag: "代码" },
    ],
  },
  {
    id: "hunyuan",
    name: "腾讯混元",
    vendor: "腾讯云",
    region: "domestic",
    baseUrl: "https://api.hunyuan.cloud.tencent.com/v1",
    docsHint: "腾讯云控制台开通混元并获取 Key",
    accent: "#0052d9",
    short: "HY",
    models: [
      { id: "hunyuan-large", label: "Hunyuan-Large", context: "256K", tag: "旗舰" },
      { id: "hunyuan-turbos-latest", label: "Hunyuan-TurboS", context: "256K", tag: "推荐" },
      { id: "hunyuan-t1", label: "Hunyuan-T1", context: "256K", tag: "推理" },
      { id: "hunyuan-a13b", label: "Hunyuan-A13B", context: "256K", tag: "极速" },
    ],
  },
  {
    id: "baichuan",
    name: "百川智能",
    vendor: "Baichuan AI",
    region: "domestic",
    baseUrl: "https://api.baichuan-ai.com/v1",
    docsHint: "platform.baichuan-ai.com 获取 Key",
    accent: "#ff6a00",
    short: "BC",
    models: [
      { id: "Baichuan4-Turbo", label: "Baichuan4 Turbo", context: "192K", tag: "旗舰" },
      { id: "Baichuan4-Air", label: "Baichuan4 Air", context: "192K", tag: "极速" },
      { id: "Baichuan4", label: "Baichuan 4", context: "192K", tag: "推荐" },
    ],
  },
  {
    id: "minimax",
    name: "MiniMax",
    vendor: "稀宇科技",
    region: "domestic",
    baseUrl: "https://api.minimax.chat/v1",
    docsHint: "platform.minimaxi.com 获取 Key",
    accent: "#e1483b",
    short: "MM",
    models: [
      { id: "MiniMax-M3", label: "MiniMax-M3", context: "1M", tag: "旗舰" },
      { id: "MiniMax-M2.5", label: "MiniMax-M2.5", context: "200K", tag: "推荐" },
      { id: "MiniMax-M2.5-highspeed", label: "MiniMax-M2.5 高速", context: "200K", tag: "极速" },
    ],
  },

  // ───────────── 国外 ─────────────
  {
    id: "openai",
    name: "OpenAI",
    vendor: "OpenAI",
    region: "foreign",
    baseUrl: "https://api.openai.com/v1",
    docsHint: "platform.openai.com 获取 API Key",
    accent: "#10a37f",
    short: "AI",
    models: [
      { id: "gpt-5.5", label: "GPT-5.5", context: "1M", tag: "旗舰" },
      { id: "gpt-5.4", label: "GPT-5.4", context: "1M", tag: "推荐" },
      { id: "gpt-5.4-mini", label: "GPT-5.4 mini", context: "400K", tag: "极速" },
      { id: "o3", label: "o3 推理", context: "200K", tag: "推理" },
    ],
  },
  {
    id: "anthropic",
    name: "Anthropic Claude",
    vendor: "Anthropic",
    region: "foreign",
    baseUrl: "https://api.anthropic.com/v1",
    docsHint: "console.anthropic.com 获取 Key",
    accent: "#d97757",
    short: "CL",
    models: [
      { id: "claude-fable-5", label: "Claude Fable 5", context: "1M", tag: "旗舰" },
      { id: "claude-opus-4-8", label: "Claude Opus 4.8", context: "1M", tag: "推荐" },
      { id: "claude-sonnet-5", label: "Claude Sonnet 5", context: "1M", tag: "推荐" },
      { id: "claude-haiku-4-5", label: "Claude Haiku 4.5", context: "200K", tag: "极速" },
    ],
  },
  {
    id: "gemini",
    name: "Google Gemini",
    vendor: "Google",
    region: "foreign",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    docsHint: "aistudio.google.com 获取 Key",
    accent: "#4285f4",
    short: "GE",
    models: [
      { id: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro", context: "1M", tag: "旗舰" },
      { id: "gemini-3-flash-preview", label: "Gemini 3 Flash", context: "1M", tag: "极速" },
      { id: "gemini-3.1-flash-lite", label: "Gemini 3.1 Flash-Lite", context: "1M", tag: "极速" },
      { id: "gemini-2.5-pro", label: "Gemini 2.5 Pro", context: "1M", tag: "稳定" },
    ],
  },
  {
    id: "xai",
    name: "xAI Grok",
    vendor: "xAI",
    region: "foreign",
    baseUrl: "https://api.x.ai/v1",
    docsHint: "console.x.ai 获取 Key",
    accent: "#e7e7e7",
    short: "XA",
    models: [
      { id: "grok-4.3", label: "Grok 4.3", context: "1M", tag: "旗舰" },
      { id: "grok-4.20", label: "Grok 4.20", context: "2M", tag: "推理" },
      { id: "grok-4.1-fast", label: "Grok 4.1 Fast", context: "2M", tag: "极速" },
    ],
  },
  {
    id: "mistral",
    name: "Mistral AI",
    vendor: "Mistral",
    region: "foreign",
    baseUrl: "https://api.mistral.ai/v1",
    docsHint: "console.mistral.ai 获取 Key",
    accent: "#fa5010",
    short: "MS",
    models: [
      { id: "mistral-large-2512", label: "Mistral Large 3", context: "262K", tag: "旗舰" },
      { id: "mistral-medium-2604", label: "Mistral Medium 3.5", context: "128K", tag: "推荐" },
      { id: "mistral-small-2603", label: "Mistral Small 4", context: "256K", tag: "极速" },
    ],
  },
  {
    id: "openrouter",
    name: "OpenRouter",
    vendor: "OpenRouter",
    region: "foreign",
    baseUrl: "https://openrouter.ai/api/v1",
    docsHint: "openrouter.ai/keys 获取 Key（聚合多家模型）",
    accent: "#8b5cf6",
    short: "OR",
    models: [
      { id: "anthropic/claude-sonnet-5", label: "Claude Sonnet 5", context: "1M", tag: "推荐" },
      { id: "openai/gpt-5.5", label: "GPT-5.5", context: "1M", tag: "旗舰" },
      { id: "google/gemini-3.1-pro-preview", label: "Gemini 3.1 Pro", context: "1M", tag: "旗舰" },
      { id: "deepseek/deepseek-v4-pro", label: "DeepSeek V4 Pro", context: "1M", tag: "高性价比" },
      { id: "x-ai/grok-4.3", label: "Grok 4.3", context: "1M", tag: "旗舰" },
    ],
  },
  {
    id: "groq",
    name: "Groq",
    vendor: "Groq",
    region: "foreign",
    baseUrl: "https://api.groq.com/openai/v1",
    docsHint: "console.groq.com 获取 Key（超低延迟）",
    accent: "#f55036",
    short: "GQ",
    models: [
      { id: "llama-3.3-70b-versatile", label: "Llama 3.3 70B", context: "128K", tag: "推荐" },
      { id: "openai/gpt-oss-120b", label: "GPT-OSS 120B", context: "128K", tag: "开源旗舰" },
      { id: "openai/gpt-oss-20b", label: "GPT-OSS 20B", context: "128K", tag: "极速" },
      { id: "meta-llama/llama-4-scout-17b-16e-instruct", label: "Llama 4 Scout 17B", context: "128K", tag: "视觉" },
    ],
  },
];

export const emptyKeyState = (): Record<string, string> =>
  Object.fromEntries(PROVIDERS.map((p) => [p.id, ""]));
