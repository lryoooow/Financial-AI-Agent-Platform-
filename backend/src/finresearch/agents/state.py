"""研究 worker 的输入/输出状态形状。

原名 FinanceGraphState，是 LangGraph 固定管线（START → entity → research → market →
summary → report → END）的图状态。图已由 deepagents 取代，五个 worker 没有作废——
它们仍是报告质量的骨架，现由 agents/deep/research_tools.py 的 prefetch_basics /
compose_report 直接调用。所以这个 TypedDict 也留着，但语义变了：
它不再是"图状态"，只是这几个 worker 之间传递的普通字典的形状说明，故改名 ResearchState。

同时删掉了 merge_findings / merge_timings 两个 reducer。它们是通过
`Annotated[list, merge_findings]` 挂给 LangGraph 状态机做通道合并的——没有图，就没有
调用者，留着只会让人误以为 findings/timings 还会自动去重合并。现在合并由调用方显式做：
findings 在 research_tools.prefetch_basics 里按顺序拼接（entity + research + market，
各 worker 产出的 key 天然不重叠）；timings 在同处用 dict 展开合并。
"""

from __future__ import annotations

from typing import List, Optional

from typing_extensions import TypedDict


class ResearchState(TypedDict, total=False):
    # 输入
    code: str            # 前端格式 600519.SH
    template: str        # equity_deep / industry / earnings / risk
    query: Optional[str]
    # 归一化
    normalized_ticker: str
    data_as_of: str
    # 任务 id（研究链路诊断 obj_id 用 task_id 而非 normalized_ticker）
    task_id: str
    # 用户画像（取数时读一次，注入报告各段撰写）
    profile: dict
    # findings 归一化视图（facts.normalize_findings 的产物）
    facts: dict
    # 结构化发现：各 worker 产出 {key, ...} 列表，由调用方拼接
    findings: list
    # 技术指标
    indicators: List[dict]
    # 风险标记
    risk_flags: List[str]
    # 报告草稿
    draft_report: Optional[dict]
    # 快速投资摘要。summary_node 先产出并推送，详细章节由 report_node 补全。
    draft_summary: Optional[str]
    # 引用来源
    citations: List[dict]
    # LLM 配置覆盖（来自前端选择的模型：{base_url, api_key, model}）；为空则回落环境变量
    llm_config: Optional[dict]
    # 阶段耗时诊断（ms）
    timings: dict
    # 错误
    error: Optional[str]
