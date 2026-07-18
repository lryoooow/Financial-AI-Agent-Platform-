"""全局配置（pydantic-settings）。

从环境变量 / .env 读取。LLM 未配置时实时端点仍工作。
数据库：留空 → 桌面默认 SQLite（单文件，落 AppData，零服务）。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 数据库：留空 → 桌面默认 SQLite；开发可设 DATABASE_URL=postgresql+psycopg://...
    database_url: Optional[str] = Field(default=None)

    # LLM（OpenAI 兼容）。留空视为未配置。
    llm_base_url: str = Field(default="https://api.deepseek.com")
    llm_api_key: Optional[str] = Field(default=None)
    llm_model: str = Field(default="deepseek-chat")
    chart_analyze_model: Optional[str] = Field(default=None)

    # 可选付费源
    tushare_token: Optional[str] = Field(default=None)
    serper_api_key: Optional[str] = Field(default=None)
    enable_serper_search: bool = Field(default=False)

    # 行情网络代理策略（根因：akshare→东方财富会继承系统/环境代理，远端常断开）
    # market_bypass_proxy=True：默认对行情域名（东方财富/Sina/Tencent）写入 NO_PROXY 绕过坏代理。
    # market_proxy 若显式设置，则行情请求走该代理（用于"确实需要代理才能出网"的环境）。
    market_bypass_proxy: bool = Field(default=True)
    market_proxy: Optional[str] = Field(default=None)

    # 服务（默认 9527 避开常见占用；桌面启动器还会自动找空闲端口）
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=9527)

    # 前端构建产物目录（桌面打包后由后端托管；dev 可指向 fronted/dist）
    frontend_dist: Optional[str] = Field(default=None)

    # agent loop 上限（对话与研究共用）。
    # 原先还有一个 recursion_limit=25，那是固定管线图的递归上限；图已移除，该项零引用，一并删掉。
    #
    # 注意这个数是 langgraph 的 **super-step**，不是工具调用次数——两者差 3 倍以上，
    # 按工具调用次数拍脑袋会严重设小（实测教训：设 12 时"茅台和五粮液比一下"必炸）。
    # 一轮工具调用 = model + TodoListMiddleware.after_model + tools = 3 步，
    # 再加开头 PatchToolCallsMiddleware.before_agent 1 步、末尾出答案 2 步。
    # N 次工具调用 = 3N + 3 步，以下为实测值（非估算）：
    #   单标的取价（resolve+quote，N=2）       9 步
    #   双标的对比（resolve/quote ×2，N=4）   15 步   ← 设 12 时就炸在这里
    #   双标的对比 + 财务（N=6）              21 步
    #   三标的 + 新闻（N=8）                  27 步
    # 30 可扛到 N=9，覆盖"行情+财务+新闻"的复杂追问，同时仍截得住真正的死循环。
    # 研究链路正常只用 2 次工具（prefetch_basics + compose_report，9 步），余量充足。
    agent_recursion_limit: int = Field(default=30)

    # 上下文预算（字符数；不引入 tiktoken：PyInstaller 联网下载 BPE 词表与离线桌面定位冲突，
    # 且国内模型 DeepSeek/Qwen/GLM 分词器与 cl100k 不一致，估出来也不准——改用经验估算）
    # context_max_chars 已移除：对话上下文预算改由 deepagents 的 SummarizationMiddleware
    # 按 token 实测管理（含历史卸载到文件系统），不再靠手写的字符数估算。
    section_facts_max_chars: int = Field(default=2400)   # 报告单段事实块预算

    # 报告多段并发撰写：家用 key 的 RPS 有限，不加闸会成片 429 → 各段静默回退模板。
    # 1.2.3：摘要改为默认走 LLM（+1 并发给 summary），四段分析不受影响。
    report_llm_concurrency: int = Field(default=4)
    report_section_timeout_s: float = Field(default=30.0)
    # 提示词中的字数要求不是协议约束；同时限制 completion token 与最终展示字符数。
    report_section_max_tokens: int = Field(default=480)
    report_section_max_chars: int = Field(default=420)
    # 摘要必须先快速可见，详细章节再并发增强；避免“摘要 60s + 章节 60s”的串行双超时。
    report_use_llm_summary: bool = Field(default=False)
    context_summary_timeout_s: float = Field(default=20.0)

    @property
    def llm_configured(self) -> bool:
        """LLM 是否可用（仅看 key）。"""
        return bool(self.llm_api_key and self.llm_api_key.strip())

    @property
    def chart_model(self) -> str:
        return (self.chart_analyze_model or self.llm_model).strip() or "gpt-4o-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def app_data_dir() -> str:
    """用户数据目录（存 SQLite、用户数据）；Windows 用 %APPDATA%/FinResearch。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "FinResearch")
    os.makedirs(d, exist_ok=True)
    return d


def resolve_db_url() -> str:
    """解析最终 DB URL：显式优先；否则桌面默认 SQLite（单文件）。"""
    if settings.database_url:
        return settings.database_url
    path = os.path.join(app_data_dir(), "finresearch.db").replace(os.sep, "/")
    return f"sqlite+aiosqlite:///{path}"
