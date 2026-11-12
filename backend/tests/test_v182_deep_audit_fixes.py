"""v1.8.2 全栈多代理深度审查回归测试套件。

覆盖：
1. 全仓 Python AST 未定义符号静态扫描（永久拦截如 entity.lookup_stock_name 未导入导致的运行期 NameError）
2. 深度研究任务列表与详情接口名称补全（股票 / ETF / 申万行业）及 ResearchTask.name 持久化，杜绝 500 导致前端无限转圈
3. SSE 与数据库 JSON 严格序列化（NaN / Infinity / NumPy 标量 / pd.NA / pd.NaT 统一净化为合法 RFC 8259 JSON）
4. /api/models 探测端口属性回退（消除 settings.api_port AttributeError）
5. /api/quote/{code}/kline 极端 n 参数钳制（消除 OverflowError）与纯图片消息校验放行
6. _start_research_runtime 终态防重入守卫（取消竞态不复活）
7. 港美股后缀拦截、万/亿单位换算、DOCX/PDF 非法 XML 控制字符清洗与 ＊ST 文件名保留
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ── 1. 全仓 AST 未定义名称静态扫描器 ─────────────────────────────────


_BUILTIN_NAMES = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__all__", "__path__"}


def _find_undefined_names_in_source(source: str, filename: str = "<mem>") -> list[tuple[int, str]]:
    """对单个 Python 模块做词法作用域分析，找出运行期必定或极可能触发 NameError 的未绑定符号。"""
    tree = ast.parse(source, filename=filename)
    future_annotations = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )

    undefined: list[tuple[int, str]] = []

    def _collect_target_names(target: ast.AST, out: set[str]) -> None:
        if isinstance(target, ast.Name):
            if isinstance(target.ctx, (ast.Store, ast.Param)):
                out.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _collect_target_names(elt, out)
        elif isinstance(target, ast.Starred):
            _collect_target_names(target.value, out)

    def _collect_scope_bindings(stmts: list[ast.stmt]) -> tuple[set[str], set[str], set[str], bool]:
        local_defs: set[str] = set()
        globals_decl: set[str] = set()
        nonlocals_decl: set[str] = set()
        has_star_import = False

        def _walk_stmt_list(nodes: list[ast.stmt]) -> None:
            nonlocal has_star_import
            for st in nodes:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    local_defs.add(st.name)
                elif isinstance(st, ast.Import):
                    for alias in st.names:
                        bound = alias.asname or alias.name.split(".")[0]
                        local_defs.add(bound)
                elif isinstance(st, ast.ImportFrom):
                    for alias in st.names:
                        if alias.name == "*":
                            has_star_import = True
                        else:
                            local_defs.add(alias.asname or alias.name)
                elif isinstance(st, ast.Assign):
                    for t in st.targets:
                        _collect_target_names(t, local_defs)
                elif isinstance(st, ast.AnnAssign) and st.target is not None:
                    _collect_target_names(st.target, local_defs)
                elif isinstance(st, ast.AugAssign):
                    _collect_target_names(st.target, local_defs)
                elif isinstance(st, (ast.For, ast.AsyncFor)):
                    _collect_target_names(st.target, local_defs)
                    _walk_stmt_list(st.body)
                    _walk_stmt_list(st.orelse)
                elif isinstance(st, ast.While):
                    _walk_stmt_list(st.body)
                    _walk_stmt_list(st.orelse)
                elif isinstance(st, (ast.With, ast.AsyncWith)):
                    for item in st.items:
                        if item.optional_vars is not None:
                            _collect_target_names(item.optional_vars, local_defs)
                    _walk_stmt_list(st.body)
                elif isinstance(st, ast.If):
                    _walk_stmt_list(st.body)
                    _walk_stmt_list(st.orelse)
                elif isinstance(st, ast.Try):
                    _walk_stmt_list(st.body)
                    for h in st.handlers:
                        if h.name:
                            local_defs.add(h.name)
                        _walk_stmt_list(h.body)
                    _walk_stmt_list(st.orelse)
                    _walk_stmt_list(st.finalbody)
                elif isinstance(st, ast.Global):
                    globals_decl.update(st.names)
                elif isinstance(st, ast.Nonlocal):
                    nonlocals_decl.update(st.names)
                elif hasattr(ast, "Match") and isinstance(st, ast.Match):
                    for case in st.cases:
                        for sub in ast.walk(case.pattern):
                            if isinstance(sub, ast.MatchAs) and sub.name:
                                local_defs.add(sub.name)
                            elif isinstance(sub, ast.MatchStar) and sub.name:
                                local_defs.add(sub.name)
                            elif isinstance(sub, ast.MatchMapping) and sub.rest:
                                local_defs.add(sub.rest)
                        _walk_stmt_list(case.body)

        def _walk_named_expr(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                            local_defs.add(sub.target.id)
                    continue
                if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                    local_defs.add(child.target.id)
                _walk_named_expr(child)

        _walk_stmt_list(stmts)
        for st in stmts:
            if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _walk_named_expr(st)
        return local_defs, globals_decl, nonlocals_decl, has_star_import

    mod_defs, _, _, mod_star = _collect_scope_bindings(tree.body)
    if mod_star:
        return []

    def _is_resolved(name: str, scopes: list[set[str]]) -> bool:
        if name in _BUILTIN_NAMES:
            return True
        for sc in reversed(scopes):
            if name in sc:
                return True
        return name in mod_defs

    def _check_node(node: ast.AST, scopes: list[set[str]], in_annotation: bool = False) -> None:
        if in_annotation and future_annotations:
            return

        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and not _is_resolved(node.id, scopes):
                undefined.append((node.lineno, node.id))
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                _check_node(dec, scopes, in_annotation)
            for d in node.args.defaults:
                _check_node(d, scopes, in_annotation)
            for d in node.args.kw_defaults:
                if d is not None:
                    _check_node(d, scopes, in_annotation)
            all_args = (
                list(node.args.posonlyargs)
                + list(node.args.args)
                + list(node.args.kwonlyargs)
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])
            )
            for a in all_args:
                if a.annotation is not None:
                    _check_node(a.annotation, scopes, True)
            if node.returns is not None:
                _check_node(node.returns, scopes, True)

            fn_defs, fn_globals, _, fn_star = _collect_scope_bindings(node.body)
            if fn_star:
                return
            param_names = {a.arg for a in all_args}
            type_params = {getattr(tp, "name", "") for tp in getattr(node, "type_params", [])} - {""}
            inner_scope = (fn_defs | param_names | type_params) - fn_globals
            next_scopes = scopes + [inner_scope]
            for st in node.body:
                _check_node(st, next_scopes, False)
            return

        if isinstance(node, ast.Lambda):
            for d in node.args.defaults:
                _check_node(d, scopes, in_annotation)
            for d in node.args.kw_defaults:
                if d is not None:
                    _check_node(d, scopes, in_annotation)
            all_args = (
                list(node.args.posonlyargs)
                + list(node.args.args)
                + list(node.args.kwonlyargs)
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])
            )
            param_names = {a.arg for a in all_args}
            _check_node(node.body, scopes + [param_names], in_annotation)
            return

        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                _check_node(dec, scopes, in_annotation)
            for b in node.bases:
                _check_node(b, scopes, in_annotation)
            for kw in node.keywords:
                _check_node(kw.value, scopes, in_annotation)
            cls_defs, _, _, cls_star = _collect_scope_bindings(node.body)
            if cls_star:
                return
            type_params = {getattr(tp, "name", "") for tp in getattr(node, "type_params", [])} - {""}
            cls_body_scopes = scopes + [cls_defs | type_params]
            for st in node.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in st.decorator_list:
                        _check_node(dec, cls_body_scopes, False)
                    for d in st.args.defaults:
                        _check_node(d, cls_body_scopes, False)
                    for d in st.args.kw_defaults:
                        if d is not None:
                            _check_node(d, cls_body_scopes, False)
                    all_args = (
                        list(st.args.posonlyargs)
                        + list(st.args.args)
                        + list(st.args.kwonlyargs)
                        + ([st.args.vararg] if st.args.vararg else [])
                        + ([st.args.kwarg] if st.args.kwarg else [])
                    )
                    for a in all_args:
                        if a.annotation is not None:
                            _check_node(a.annotation, cls_body_scopes, True)
                    if st.returns is not None:
                        _check_node(st.returns, cls_body_scopes, True)
                    fn_defs, fn_globals, _, fn_star = _collect_scope_bindings(st.body)
                    if not fn_star:
                        fn_type_params = {getattr(tp, "name", "") for tp in getattr(st, "type_params", [])} - {""}
                        inner = (fn_defs | {a.arg for a in all_args} | type_params | fn_type_params) - fn_globals
                        for sub_st in st.body:
                            _check_node(sub_st, scopes + [inner], False)
                else:
                    _check_node(st, cls_body_scopes, False)
            return

        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            comp_scopes = list(scopes)
            for gen in node.generators:
                _check_node(gen.iter, comp_scopes, in_annotation)
                gen_names: set[str] = set()
                _collect_target_names(gen.target, gen_names)
                comp_scopes = comp_scopes + [gen_names]
                for if_clause in gen.ifs:
                    _check_node(if_clause, comp_scopes, in_annotation)
            if isinstance(node, ast.DictComp):
                _check_node(node.key, comp_scopes, in_annotation)
                _check_node(node.value, comp_scopes, in_annotation)
            else:
                _check_node(node.elt, comp_scopes, in_annotation)
            return

        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                _check_node(node.value, scopes, in_annotation)
            _check_node(node.annotation, scopes, True)
            if not isinstance(node.target, ast.Name):
                _check_node(node.target, scopes, in_annotation)
            return

        for child in ast.iter_child_nodes(node):
            _check_node(child, scopes, in_annotation)

    for stmt in tree.body:
        _check_node(stmt, [mod_defs], False)

    return undefined


def test_all_backend_modules_have_no_undefined_names():
    # 先自检分析器能精准抓出此前导致 500 的未导入符号
    bad_src = "def _research_task_meta(t):\n    return entity.lookup_stock_name(t.code)\n"
    detected = _find_undefined_names_in_source(bad_src)
    assert any(name == "entity" for _, name in detected)

    src_root = Path(__file__).resolve().parent.parent / "src" / "finresearch"
    py_files = sorted(src_root.rglob("*.py"))
    assert len(py_files) >= 35

    violations: list[str] = []
    for py_file in py_files:
        text = py_file.read_text(encoding="utf-8")
        missing = _find_undefined_names_in_source(text, filename=py_file.name)
        for lineno, sym in missing:
            rel = py_file.relative_to(src_root).as_posix()
            violations.append(f"{rel}:{lineno} undefined '{sym}'")

    assert violations == [], "发现未定义符号：\n" + "\n".join(violations)


# ── 2. 深度研究列表/详情名称解析与无 500 验证 ────────────────────────


def test_research_task_meta_resolves_names_without_500():
    from finresearch.api.routes import _research_task_meta
    from finresearch.db.models import ResearchTask

    for status in ("pending", "running", "done", "error"):
        t_stock = ResearchTask(
            id=f"task-{status}",
            code="600519.SH",
            name="",
            template="equity_deep",
            status=status,
            created_at=datetime(2026, 11, 11, 10, 0, 0),
        )
        meta = _research_task_meta(t_stock)
        assert meta["status"] == status
        assert meta["name"] == "贵州茅台"

    t_etf = ResearchTask(
        id="task-etf",
        code="510300.SH",
        name="",
        template="equity_deep",
        status="done",
        created_at=datetime(2026, 11, 11, 10, 2, 0),
    )
    assert "300" in _research_task_meta(t_etf)["name"]

    t_ind = ResearchTask(
        id="task-industry",
        code="半导体",
        name="半导体",
        template="industry",
        status="done",
        created_at=datetime(2026, 11, 11, 10, 3, 0),
    )
    assert _research_task_meta(t_ind)["name"] == "半导体"

    t_custom = ResearchTask(
        id="task-custom",
        code="688981.SH",
        name="中芯国际",
        template="equity_deep",
        status="done",
        report_id="rep-42",
        created_at=datetime(2026, 11, 11, 10, 5, 0),
    )
    meta_custom = _research_task_meta(t_custom)
    assert meta_custom["name"] == "中芯国际"
    assert meta_custom["report_id"] == "rep-42"


# ── 3. SSE 与数据库 JSON 严格序列化（杜绝 NaN / Infinity）───────────


def test_clean_for_json_and_sse_reject_nan_inf():
    from finresearch.api.sse import sse
    from finresearch.tools import clean_for_json

    dirty_payload = {
        "pe": float("nan"),
        "pb": float("inf"),
        "neg_inf": float("-inf"),
        "np_int": np.int64(42),
        "np_nan": np.float64("nan"),
        "np_val": np.float64(3.14),
        "pd_na": pd.NA,
        "pd_nat": pd.NaT,
        "ts": datetime(2026, 11, 11, 12, 30, 0),
        "nested": [1.5, float("nan"), {"ok": True}],
    }
    cleaned = clean_for_json(dirty_payload)
    assert cleaned["pe"] is None
    assert cleaned["pb"] is None
    assert cleaned["neg_inf"] is None
    assert cleaned["np_int"] == 42 and isinstance(cleaned["np_int"], int)
    assert cleaned["np_nan"] is None
    assert cleaned["np_val"] == pytest.approx(3.14)
    assert cleaned["pd_na"] is None
    assert cleaned["pd_nat"] is None
    assert cleaned["nested"] == [1.5, None, {"ok": True}]

    frame = sse({"type": "done", "report": dirty_payload})
    assert frame.startswith("data: ")
    raw_json = frame[len("data: ") :].strip()

    def _reject_non_standard_const(token: str):
        raise ValueError(f"Non-RFC8259 constant leaked: {token}")

    parsed = json.loads(raw_json, parse_constant=_reject_non_standard_const)
    assert parsed["type"] == "done"
    assert parsed["report"]["pe"] is None
    assert parsed["report"]["pd_nat"] is None
    assert parsed["report"]["np_int"] == 42


# ── 4. /api/models 端口回退、K 线极端 n 钳制与纯图片消息校验 ─────────


def test_list_provider_models_no_api_port_attribute_error(monkeypatch):
    import urllib.request as _u
    from finresearch.api import routes

    class _FakeHTTPResp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _n: int = -1) -> bytes:
            return json.dumps({"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}).encode("utf-8")

    class _FakeOpener:
        def open(self, req_obj, timeout=15):
            return _FakeHTTPResp()

    monkeypatch.setattr(_u, "build_opener", lambda *args, **kwargs: _FakeOpener())
    body = routes.ModelsRequest(base_url="https://1.1.1.1/v1", api_key="sk-test-123456")
    out = asyncio.run(routes.list_models(body, request=None))
    assert [m["id"] for m in out["models"]] == ["deepseek-chat", "deepseek-reasoner"]


def test_kline_clamps_extreme_n_and_empty_chat_validation(monkeypatch):
    from fastapi import HTTPException
    from finresearch.api import routes

    captured_args: list[tuple[str, int, str]] = []

    def _fake_get_kline(code: str, n: int = 60, adjust: str = "qfq"):
        captured_args.append((code, n, adjust))
        return {
            "data": [{"date": "2026-11-10", "open": 10.0, "high": 11.0, "low": 9.8, "close": 10.5, "volume": 1000}],
            "provenance": {"source": "test"},
        }

    monkeypatch.setattr(routes, "get_kline", _fake_get_kline)
    res_huge = asyncio.run(routes.kline("600519.SH", n=999_999_999, adjust="qfq"))
    assert captured_args[-1] == ("600519.SH", 1000, "qfq")
    assert len(res_huge["data"]) == 1

    asyncio.run(routes.kline("600519.SH", n=-50, adjust="invalid"))
    assert captured_args[-1] == ("600519.SH", 1, "qfq")

    # 空文本 + 无图片 -> 400
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes.send_chat_message("s-nonexistent", routes.ChatSendRequest(message="   ", images=[])))
    assert exc_info.value.status_code == 400


# ── 5. _start_research_runtime 终态守卫防取消竞态复活 ────────────────


def test_research_runtime_terminal_guard_prevents_cancel_resurrection():
    from finresearch.api import routes

    tid = "test-terminal-guard-001"
    rt = routes._ResearchRuntime(task_id=tid)
    rt.terminal = True
    routes._RESEARCH_RUNS[tid] = rt
    spawned = False

    async def _factory():
        nonlocal spawned
        spawned = True
        if False:
            yield ""

    async def _run():
        try:
            got = routes._start_research_runtime(tid, _factory)
            assert got is rt
            assert got.task is None
            await asyncio.sleep(0)
            assert not spawned
        finally:
            routes._RESEARCH_RUNS.pop(tid, None)

    asyncio.run(_run())


# ── 6. 港美股后缀拦截、万/亿换算、DOCX/PDF XML 控制字符清洗 ─────────


def test_entity_non_ashare_guard_facts_wan_and_export_xml_safety():
    from finresearch.tools import export, facts
    from finresearch.tools.entity import resolve_entity

    # 00700.HK / AAPL.US 不得误匹配到 A 股代码前缀
    hk = resolve_entity("00700.HK", limit=5)
    hk_items = hk.get("results", []) if isinstance(hk, dict) else hk
    assert hk_items == []

    us = resolve_entity("AAPL.US", limit=5)
    us_items = us.get("results", []) if isinstance(us, dict) else us
    assert us_items == []

    # 财务单位换算（统一折算为「亿元」口径）：8500万 == 0.85亿，1.25亿 == 1.25亿
    assert facts.to_float("8500万") == pytest.approx(0.85)
    assert facts.to_float("1.25亿") == pytest.approx(1.25)

    # 全角星号 ＊ST 文件名保留有效汉字主体
    fn = export.safe_filename({"name": "＊ST景峰", "code": "000908.SZ"}, "docx")
    assert "ST景峰" in fn
    assert fn.endswith(".docx")

    # 含非法 XML 1.0 控制字符（\x00 / \x07 / \x0b / \x1f）及多行风险项时 DOCX 与 PDF 均正常生成
    sample_report = {
        "code": "600519.SH",
        "name": "贵州茅台\x00\x07",
        "rating": "核心关注",
        "createdAt": "2026-11-11 18:00",
        "summary": "营收稳健\x0b增长，现金流充沛\x1f。",
        "metrics": {"price": 1520.0, "changePct": 1.25, "pe": 0, "pb": 0, "marketCap": "1.91万亿"},
        "sections": [
            {"title": "经营基本面\x01", "content": "- 核心产品动销良性\x07\n- 渠道库存健康"},
        ],
        "risks": ["宏观消费复苏节奏波动\n终端批价短期震荡"],
        "sources": [{"title": "2026年三季报\x02\n上海证券交易所", "time": "2026-10-28"}],
        "disclaimer": "本报告仅供研究参考，不构成投资建议。",
    }
    docx_bytes = export.render_docx(sample_report)
    assert docx_bytes[:2] == b"PK" and len(docx_bytes) > 1000

    pdf_bytes = export.render_pdf(sample_report)
    assert pdf_bytes.startswith(b"%PDF") and len(pdf_bytes) > 1000

    md_str = export.render_md(sample_report)
    assert "贵州茅台" in md_str

    from finresearch.tools import report as report_mod

    valid_doc = report_mod.build_doc(
        ticker="600519.SH",
        name="贵州茅台",
        template="equity_deep",
        summary="营收稳健增长。",
        catalyst="旺季动销超预期",
        financials={"years": ["2025"], "rows": [{"name": "ROE", "values": ["30.5%"]}]},
        sections=[{"title": "经营基本面", "body": "渠道库存健康。"}],
        risks=["宏观消费复苏节奏波动"],
        sources=[{"title": "2026年三季报", "url": "", "date": "2026-10-28"}],
        quote={"price": 1520.0, "change": 12.0, "change_pct": 0.8, "pe": 0, "pb": 0},
    )
    html_str = report_mod.render_html(valid_doc)
    # quote.pe=0 / quote.pb=0 在模板中应渲染为破折号而非 0，且兼容 change_pct
    assert '<div class="label">PE（动态）</div><div class="num">—</div>' in html_str
    assert '<div class="label">PB</div><div class="num">—</div>' in html_str
    assert "0.8%" in html_str
