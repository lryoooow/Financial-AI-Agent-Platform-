"""统一品牌资产生成器（dev-time only，用 Pillow）。

品牌：景渊 JingYuan · 金融研究助手
设计方向「深潭与升线」（dark fintech）：
- 圆角方形容器：深海军蓝纵向渐变（#0B1220 → #182238），与 theme.css 的 .dark 主题同色。
- 底部三道同心弧：由内向外递暗，读作"渊"——深潭与涟漪。
- 主体：三根金色 K 线柱自潭中升起，自左至右依次增高、依次提亮（#D8B24A → #F0D27A）。
- 点睛：一根亮金折线串联三峰，强化上行动势与"研判"的连贯性。
- 无文字：16px 任务栏图标也要可读。

小尺寸降级（关键）：弧线在 ≤32px 下会糊成一条脏边，因此 16/24/32 三档用
_draw_mark(simplified=True) —— 去掉弧线与端点圆，只留三柱 + 折线。

产出（运行 `python -m finresearch.tools.make_brand_assets`）：
- fronted/build/icon.ico            多分辨率（含 256×256，electron-builder / NSIS 必需）
- fronted/build/icon-512.png        大尺寸预览
- fronted/public/favicon.ico        浏览器标签图标
- fronted/src/app/assets/logo-mark.svg   应用内 Logo（矢量，TitleBar / Sidebar）
- fronted/src/app/assets/logo-256.png
- backend/packaging/app.ico         PyInstaller exe 图标
- fronted/electron/icon.ico         运行时窗口/任务栏图标

注意：所有 PIL import 必须放在函数内 —— PyInstaller 的 collect_submodules("finresearch")
会静态扫描本模块，若顶层 import PIL 会触发对被排除的 PIL 的解析告警。运行期 App 永不导入本模块。
"""

from __future__ import annotations

import math

# 逻辑坐标系：1024×1024。所有几何按比例推导，缩放到任意尺寸都自洽。
_S = 1024

# —— 配色（与 fronted/src/styles/theme.css 的 .dark 主题一致）——
_NAVY_TOP = (0x0B, 0x12, 0x20)
_NAVY_BOT = (0x18, 0x22, 0x38)
_GOLD = (0xD8, 0xB2, 0x4A)
_GOLD_BRIGHT = (0xF0, 0xD2, 0x7A)
_RADIUS = int(_S * 0.225)

# —— 深潭：三道同心弧，圆心在柱脚上方，弧线向下兜成"∪"，越深越淡 ——
# 弧必须整体落在柱脚之下，否则端点会横穿最外侧的柱体。
_POOL_CX, _POOL_CY = int(_S * 0.500), int(_S * 0.600)
# (rx, ry, width, alpha)：由浅入深，逐道变宽、变细、变淡
_POOL_ARCS = [
    (int(_S * 0.250), int(_S * 0.090), int(_S * 0.020), 255),
    (int(_S * 0.330), int(_S * 0.125), int(_S * 0.014), 140),
    (int(_S * 0.410), int(_S * 0.160), int(_S * 0.010), 70),
]
_ARC_START, _ARC_END = 30, 150  # PIL：0°=3点钟、顺时针；30→90→150 经过正下方，成"∪"

# —— 三根 K 线柱：自左至右增高，柱底齐平坐在潭面之上；影线只在柱顶（下影线会戳进潭里） ——
_BODY_BOTTOM = int(_S * 0.615)
# (center_x, wick_high_y, body_top_y)
_CANDLES = [
    (int(_S * 0.327), int(_S * 0.472), int(_S * 0.500)),
    (int(_S * 0.500), int(_S * 0.372), int(_S * 0.400)),
    (int(_S * 0.673), int(_S * 0.267), int(_S * 0.295)),
]
_BODY_W = int(_S * 0.090)
_WICK_W = int(_S * 0.016)
_LINE_W = int(_S * 0.012)
_DOT_R = _S * 0.019


def _candle_color(i: int) -> tuple:
    """自左至右依次提亮：末根用亮金强调"当前"。"""
    return _GOLD_BRIGHT if i == len(_CANDLES) - 1 else _GOLD


def _gradient_bg():
    """深海军蓝纵向渐变圆角方形（逐行填充，1024 行一次性成本可忽略）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (_S, _S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(_S):
        t = y / (_S - 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(_NAVY_TOP, _NAVY_BOT))
        d.line([(0, y), (_S, y)], fill=(*c, 255))
    mask = Image.new("L", (_S, _S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, _S - 1, _S - 1), radius=_RADIUS, fill=255)
    out = Image.new("RGBA", (_S, _S), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _arc_layer(rx: int, ry: int, width: int, alpha: int):
    """单道弧画在独立透明层上，再整体降透明度。

    不能直接 d.arc(fill=(r,g,b,alpha))：ImageDraw.arc **忽略 fill 里的 alpha 通道**
    （实测三道弧全部渲染成同一个满不透明的 #D8B24A），必须靠 alpha_composite 分层。
    """
    from PIL import Image, ImageDraw

    layer = Image.new("RGBA", (_S, _S), (0, 0, 0, 0))
    ImageDraw.Draw(layer).arc(
        (_POOL_CX - rx, _POOL_CY - ry, _POOL_CX + rx, _POOL_CY + ry),
        _ARC_START,
        _ARC_END,
        fill=(*_GOLD, 255),
        width=width,
    )
    if alpha < 255:
        layer.putalpha(layer.getchannel("A").point(lambda v: v * alpha // 255))
    return layer


def _draw_mark(simplified: bool = False):
    """在渐变背景上绘制深潭 + 三连柱 + 升线，返回 1024 RGBA。

    simplified=True 用于 ≤32px 图标：省略同心弧、上影线与端点圆，只保留可读的三柱与折线。
    """
    from PIL import Image, ImageDraw

    img = _gradient_bg()
    if not simplified:
        for rx, ry, width, alpha in _POOL_ARCS:
            img = Image.alpha_composite(img, _arc_layer(rx, ry, width, alpha))

    d = ImageDraw.Draw(img, "RGBA")
    for i, (cx, wick_high, body_top) in enumerate(_CANDLES):
        color = _candle_color(i)
        if not simplified:  # 上影线：淡金细竖条，只在柱顶，不向下穿潭
            d.rounded_rectangle(
                (cx - _WICK_W // 2, wick_high, cx + _WICK_W // 2, body_top),
                radius=_WICK_W // 2,
                fill=(*color, 130),
            )
        d.rounded_rectangle(
            (cx - _BODY_W // 2, body_top, cx + _BODY_W // 2, _BODY_BOTTOM),
            radius=int(_BODY_W * 0.22),
            fill=color,
        )

    pts = [(cx, wick_high) for (cx, wick_high, _bt) in _CANDLES]
    d.line(pts, fill=_GOLD_BRIGHT, width=_LINE_W, joint="curve")
    if not simplified:
        # 只在最高点留一个端点作"当前"。三个点会让细影线看起来像地图图钉。
        cx, wy = pts[-1]
        d.ellipse((cx - _DOT_R, wy - _DOT_R, cx + _DOT_R, wy + _DOT_R), fill=_GOLD_BRIGHT)
    return img


def _svg_mark() -> str:
    """与位图同构的矢量 Logo（应用内 TitleBar / Sidebar / README 头图，任意缩放清晰）。

    同心弧用二次贝塞尔而非 <path A>：弧的 sweep-flag 在 y 轴向下的坐标系里极易画反，
    而经过潭底的二次贝塞尔可以由端点与中点解析求出控制点，无歧义。
    """
    parts: list[str] = []

    # 深潭：三道同心弧。端点在 θ=_ARC_END 与 θ=_ARC_START，中点落在潭底 (cx, cy+ry)。
    for rx, ry, width, alpha in _POOL_ARCS:
        x1 = _POOL_CX + rx * math.cos(math.radians(_ARC_END))
        x2 = _POOL_CX + rx * math.cos(math.radians(_ARC_START))
        y_end = _POOL_CY + ry * math.sin(math.radians(_ARC_START))
        # 二次贝塞尔中点 = 0.5*端点均值 + 0.5*控制点 → 反解控制点使中点恰为潭底
        y_ctrl = 2 * (_POOL_CY + ry) - y_end
        parts.append(
            f'<path d="M {x1:.1f} {y_end:.1f} Q {_POOL_CX} {y_ctrl:.1f} {x2:.1f} {y_end:.1f}" '
            f'fill="none" stroke="#D8B24A" stroke-width="{width}" '
            f'stroke-linecap="round" opacity="{alpha / 255:.2f}"/>'
        )

    for i, (cx, wick_high, body_top) in enumerate(_CANDLES):
        color = "#F0D27A" if i == len(_CANDLES) - 1 else "#D8B24A"
        parts.append(
            f'<rect x="{cx - _WICK_W / 2:.1f}" y="{wick_high}" width="{_WICK_W}" '
            f'height="{body_top - wick_high}" rx="{_WICK_W / 2}" fill="{color}" opacity="0.51"/>'
        )
        parts.append(
            f'<rect x="{cx - _BODY_W / 2:.1f}" y="{body_top}" width="{_BODY_W}" '
            f'height="{_BODY_BOTTOM - body_top}" rx="{_BODY_W * 0.22:.1f}" fill="{color}"/>'
        )

    pts = " ".join(f"{cx},{wy}" for (cx, wy, _bt) in _CANDLES)
    parts.append(
        f'<polyline points="{pts}" fill="none" stroke="#F0D27A" '
        f'stroke-width="{_LINE_W}" stroke-linejoin="round" stroke-linecap="round"/>'
    )
    top_cx, top_wy, _ = _CANDLES[-1]
    parts.append(f'<circle cx="{top_cx}" cy="{top_wy}" r="{_DOT_R:.1f}" fill="#F0D27A"/>')

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" fill="none" '
        'role="img" aria-label="JingYuan">\n'
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">\n'
        f'<stop offset="0" stop-color="#{_NAVY_TOP[0]:02X}{_NAVY_TOP[1]:02X}{_NAVY_TOP[2]:02X}"/>\n'
        f'<stop offset="1" stop-color="#{_NAVY_BOT[0]:02X}{_NAVY_BOT[1]:02X}{_NAVY_BOT[2]:02X}"/>\n'
        '</linearGradient></defs>\n'
        f'<rect width="1024" height="1024" rx="{_RADIUS}" fill="url(#bg)"/>\n'
        + "\n".join(parts)
        + "\n</svg>\n"
    )


def _save_ico(path: str, sizes) -> None:
    """多尺寸 ICO：≤32px 用简化版渲染，避免同心弧在小图里糊成脏边。

    基准图必须是**最大**的一档：Pillow 保存 ICO 时会丢弃所有大于基准图的尺寸，
    拿 16×16 当基准会得到一个只含 16×16 的 ico（NSIS 与任务栏都要 256×256）。
    其余各档经 append_images 传入，Pillow 按尺寸精确匹配。
    """
    from PIL import Image

    full = _draw_mark(simplified=False)
    small = _draw_mark(simplified=True)
    ordered = sorted(sizes, reverse=True)
    frames = [(full if s > 32 else small).resize((s, s), Image.LANCZOS) for s in ordered]
    frames[0].save(
        path, format="ICO", sizes=[(s, s) for s in ordered], append_images=frames[1:]
    )


def main() -> None:
    import os

    from PIL import Image

    here = os.path.dirname(os.path.abspath(__file__))
    proj_root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))  # → D:\Lry\langchain
    fronted = os.path.join(proj_root, "fronted")
    build_dir = os.path.join(fronted, "build")
    public_dir = os.path.join(fronted, "public")
    assets_dir = os.path.join(fronted, "src", "app", "assets")
    electron_dir = os.path.join(fronted, "electron")
    packaging_dir = os.path.join(proj_root, "backend", "packaging")
    for d in (build_dir, public_dir, assets_dir, electron_dir, packaging_dir):
        os.makedirs(d, exist_ok=True)

    img = _draw_mark()
    img.resize((512, 512), Image.LANCZOS).save(os.path.join(build_dir, "icon-512.png"))
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(assets_dir, "logo-256.png"))
    img.save(os.path.join(build_dir, "logo-1024.png"))

    ico_sizes = (16, 24, 32, 48, 64, 128, 256)
    _save_ico(os.path.join(build_dir, "icon.ico"), ico_sizes)
    _save_ico(os.path.join(packaging_dir, "app.ico"), ico_sizes)
    _save_ico(os.path.join(electron_dir, "icon.ico"), ico_sizes)
    _save_ico(os.path.join(public_dir, "favicon.ico"), (16, 32, 48))

    with open(os.path.join(assets_dir, "logo-mark.svg"), "w", encoding="utf-8") as f:
        f.write(_svg_mark())

    print("[brand] 景渊 JingYuan — generated:")
    for p in (
        os.path.join(build_dir, "icon.ico"),
        os.path.join(build_dir, "icon-512.png"),
        os.path.join(build_dir, "logo-1024.png"),
        os.path.join(packaging_dir, "app.ico"),
        os.path.join(electron_dir, "icon.ico"),
        os.path.join(public_dir, "favicon.ico"),
        os.path.join(assets_dir, "logo-256.png"),
        os.path.join(assets_dir, "logo-mark.svg"),
    ):
        print("  -", os.path.relpath(p, proj_root))


if __name__ == "__main__":
    main()
