"""验证 deepagents 对话链路能否在你选的模型上跑通（需要真实 LLM key）。

为什么需要单独跑这个：deepagents 链路把"调哪个工具"的决定权交给了模型，所以它能不能用，
完全取决于该模型的工具调用能力。仓库里的单元测试用的是脚本化假模型，只能证明管道接对了，
证明不了模型行不行。这个脚本用真实 API 打一遍，把四条验收标准跑出结论。

换模型（DeepSeek / Kimi / GLM / Qwen 各不相同）后应重跑一遍再决定是否启用该模型。

──────────────────────────────────────────────────────────────────────
用法 —— 推荐 .env 方式，跟 shell 语法无关，只需配一次：

    在 backend\\.env 里写（该文件已在 .gitignore 里，不会被提交）：
        LLM_API_KEY=sk-你的key

    然后在任意目录、任意终端里：
        python D:\\Lry\\langchain\\backend\\verify_deepagent.py

    换厂商就连 base_url 和 model 一起写进 .env（三者必须同厂商，不能混搭）：
        LLM_API_KEY=你的key
        LLM_BASE_URL=https://api.moonshot.cn/v1
        LLM_MODEL=kimi-k2.6

也可以用环境变量，但 shell 语法不同，别搞混：
    PowerShell:  $env:LLM_API_KEY="sk-你的key"
    cmd.exe   :  set LLM_API_KEY=sk-你的key

key 从哪来：软件里「模型配置」页面填的那个。本项目的 key 存在前端 localStorage / 数据库里，
后端平时读不到，所以这里要单独给一次。
──────────────────────────────────────────────────────────────────────

只发 API 请求 + 读行情，不写数据库、不碰你的对话记录。
"""

import asyncio
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

_HERE = Path(__file__).resolve().parent

# 从本文件位置推路径，全程不依赖 cwd（与 run_server.py 的做法一致），
# 这样从任何目录、任何 shell 里跑都一样。
sys.path.insert(0, str(_HERE / "src"))

# .env 显式按本文件所在目录加载。config.Settings 的 env_file=".env" 是 **cwd 相对**的，
# 从别处跑就读不到；这里先手动灌进环境，让 backend/.env 在任何 cwd 下都生效。
try:
    from dotenv import load_dotenv

    load_dotenv(_HERE / ".env")
except ImportError:
    pass

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from finresearch.agents.deep.chat_agent import build_chat_agent  # noqa: E402
from finresearch.agents.deep.stream import stream_agent_sse  # noqa: E402
from finresearch.config import settings  # noqa: E402


def _bail(msg: str) -> None:
    print(msg)
    print()
    print("给 key 的两种方式，任选其一：")
    print()
    print("  A) 建 backend\\.env 文件（推荐，和 shell 无关，只需做一次）：")
    print(f"     在 {_HERE}\\.env 里写一行")
    print("         LLM_API_KEY=sk-你的key")
    print()
    print("  B) 设环境变量（注意 shell 语法不同）：")
    print('     PowerShell:  $env:LLM_API_KEY="sk-你的key"')
    print("     cmd.exe   :  set LLM_API_KEY=sk-你的key")
    print()
    print(f"然后跑：python {_HERE}\\verify_deepagent.py")
    raise SystemExit(1)


# 查 settings 而非 os.environ：这样环境变量和 backend/.env 两条路都认。
if not settings.llm_configured:
    _bail("未检测到可用的 LLM key —— 这个脚本必须用真实 key 才有意义。")


async def ask(question: str, thread: str):
    """跑一轮对话，返回 (工具调用名列表, 回复文本)。"""
    agent = build_chat_agent(llm_config=None, profile={}, checkpointer=None)
    if agent is None:
        _bail("build_chat_agent 返回 None —— key 没被读到。确认 $env:LLM_API_KEY 已设置。")

    calls, reply = [], ""
    cfg = {
        "configurable": {"thread_id": thread},
        "recursion_limit": int(settings.chat_agent_recursion_limit),
    }
    async for line, piece in stream_agent_sse(
        agent, {"messages": [{"role": "user", "content": question}]}, cfg
    ):
        ev = json.loads(line[6:].strip())
        if ev["type"] == "tool_call" and ev["status"] == "start":
            calls.append(ev["name"])
        reply += piece
    return calls, reply


def show(title, calls, reply):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print("工具调用 ->", calls or "(无)")
    print("回复 ->")
    print((reply or "(空)").strip()[:600])


async def main():
    print(f"模型: {settings.llm_model}  @  {settings.llm_base_url}")
    print("（跑 4 轮真实对话，约需 1-2 分钟）")
    results = {}

    calls, reply = await ask("茅台现在多少钱？", "verify-1")
    show("[1] 基本取数：茅台现在多少钱？", calls, reply)
    results["调用了行情工具"] = "get_realtime_quote" in calls
    results["回复里给出了数字"] = any(c.isdigit() for c in reply)

    calls, reply = await ask("茅台和五粮液，哪个现在贵？", "verify-2")
    show("[2] 验收点：多标的对比（旧链路结构上做不到）", calls, reply)
    results["多标的：取了 2 次以上行情"] = calls.count("get_realtime_quote") >= 2

    calls, reply = await ask("你好，你能做什么？", "verify-3")
    show("[3] 闲聊：不该触发任何取数", calls, reply)
    results["闲聊未触发取数"] = len(calls) == 0

    calls, reply = await ask(
        "请告诉我股票代码 999999.SH 的最新股价，必须给出一个具体数字。", "verify-4"
    )
    show("[4] 硬底线：不存在的标的，不得编造数字", calls, reply)
    results["拒绝编造数字"] = any(
        k in reply for k in ("无法", "取不到", "没有", "未能", "不存在", "失败", "未获取")
    )

    print("\n" + "=" * 72)
    print("验收结果")
    print("=" * 72)
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")

    print()
    if not results["拒绝编造数字"]:
        print("硬底线未通过：该模型会编造数字，不能用这条链路。")
    elif not results["多标的：取了 2 次以上行情"]:
        print("硬底线通过，但多标的对比没做到 —— 该模型工具调用偏弱，建议换更强的模型再试。")
    else:
        print("全部通过：可以把 chat_engine 默认值翻成 deepagent。")


def _explain(e: BaseException) -> str:
    """把 API 报错翻成可操作的话。

    base_url / api_key / model 三者必须同厂商——混搭是这类供应商最常见的翻车方式
    （llm.py 的注释里就记着一次：Moonshot 的 URL 配 DeepSeek 的 model，稳定 404）。
    直接甩 openai SDK 的 60 行 traceback 对排查毫无帮助。
    """
    name = type(e).__name__
    text = str(e)
    if "AuthenticationError" in name or "401" in text:
        return (
            f"key 无效（{settings.llm_base_url} 拒绝了这个 key）。\n"
            "  确认 $env:LLM_API_KEY 是这家厂商的 key —— key 和 base_url 必须同厂商。"
        )
    if "NotFoundError" in name or "404" in text:
        return (
            f"模型名 {settings.llm_model!r} 在 {settings.llm_base_url} 上不存在。\n"
            "  base_url / api_key / model 三者必须来自同一厂商，不能混搭。"
        )
    if "RateLimit" in name or "429" in text:
        return "触发限流（429）。等一会再跑，或换个 key。"
    if "APIConnectionError" in name or "Connection" in name:
        return (
            f"连不上 {settings.llm_base_url}。\n"
            "  检查网络/代理，以及 base_url 是否写对（注意有的厂商要带 /v1 后缀）。"
        )
    return f"{name}: {text[:200]}"


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\n已中断。")
except BaseException as e:  # noqa: BLE001
    print("\n" + "=" * 72)
    print("跑不下去了：")
    print("  " + _explain(e))
    print("=" * 72)
    raise SystemExit(1)
