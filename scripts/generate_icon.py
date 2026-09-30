"""生成应用图标（Apple 风格圆角方块 + 货币符号）。

为什么用脚本生成而不是直接放一个二进制图标
------------------------------------------
1. **可复现**：任何人都能重新生成同一份图标，仓库里不必存进"来路不明的二进制"；
2. **可调整**：改主色只需改这里的几个常量，随即全局生效（窗口图标、托盘图标、安装包图标）；
3. **可审计**：图标完全由几何图形绘制，**不含任何第三方商标素材**，
   避开了商业字体与品牌图形的授权风险（见 docs/CARD_ART_GUIDE.md 的同类约定）。

输出
----
    src/accountbook/resources/icons/app.ico   ← Windows 多尺寸图标（16–256）
    src/accountbook/resources/icons/app.png   ← 256px 预览图（文档与「关于」页使用）

用法::

    python scripts/generate_icon.py
    python scripts/generate_icon.py --accent "#0A84FF" --accent2 "#5856D6"
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

#: 与前端设计令牌保持一致的主色（系统蓝 → 靛蓝渐变），保证应用内外的观感统一
DEFAULT_ACCENT = (10, 132, 255)
DEFAULT_ACCENT_2 = (88, 86, 214)

#: Windows 图标需要多尺寸：16/24/32 用于任务栏与资源管理器小图标，
#: 48/64 用于中等视图，128/256 用于大图标与安装程序。
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: 绘制超采样倍率。先在 4 倍尺寸上绘制再缩放，得到平滑的圆角与线条边缘
#: （Pillow 的 draw 不做抗锯齿，这是最实用的替代方案）。
SUPERSAMPLE = 4

#: 圆角半径占边长的比例 —— 接近 iOS/macOS 的"squircle"观感
CORNER_RATIO = 0.225


def _lerp(start: int, end: int, ratio: float) -> int:
    """线性插值（整数)。"""
    return round(start + (end - start) * ratio)


def _gradient_background(size: int, accent: tuple[int, int, int], accent2: tuple[int, int, int]) -> Image.Image:
    """绘制对角线性渐变的圆角方块背景。

    实现方式：先逐行生成渐变矩形，再用圆角遮罩裁剪。
    对角线渐变比纯水平/垂直渐变更有体积感，是 Apple 图标常见的处理。
    """
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size))
    pixels = gradient.load()
    assert pixels is not None

    # 对角线渐变：t = (x + y) / (2 * size)，从左上角的主色过渡到右下角的副色
    for y in range(size):
        for x in range(size):
            ratio = (x + y) / (2 * size - 2) if size > 1 else 0.0
            pixels[x, y] = (
                _lerp(accent[0], accent2[0], ratio),
                _lerp(accent[1], accent2[1], ratio),
                _lerp(accent[2], accent2[2], ratio),
                255,
            )

    # 圆角遮罩：超采样后的半径按比例放大
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=int(size * CORNER_RATIO),
        fill=255,
    )
    canvas.paste(gradient, (0, 0), mask)
    return canvas


def _draw_glyph(canvas: Image.Image) -> None:
    """绘制居中的白色货币符号（¥）。

    符号完全由直线上色构成，因此：
        * 不依赖任何字体文件，跨机器结果完全一致；
        * 在 16px 下依然能被辨认（笔画粗、结构简单）。
    """
    size = canvas.width
    draw = ImageDraw.Draw(canvas)
    center = size / 2

    # 笔画宽度按边长比例：16px 图标下约 1.6px（经缩放后仍可辨认）
    stroke = max(2, int(size * 0.085))
    white = (255, 255, 255, 255)

    # 字形包围盒（相对边长的比例），留出足够的内边距 —— Apple 图标不会顶满画布
    half_width = size * 0.175
    top = size * 0.245
    mid = size * 0.44
    bottom = size * 0.755

    def line(x1: float, y1: float, x2: float, y2: float) -> None:
        draw.line((x1, y1, x2, y2), fill=white, width=stroke, joint="curve")
        # 圆头端点：手动补圆，因为 Pillow 的 line 没有 linecap 选项
        radius = stroke / 2
        for cx, cy in ((x1, y1), (x2, y2)):
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=white)

    # ---- ¥ 的上半部：两条斜线汇于中点 --------------------------------------
    line(center - half_width, top, center, mid)
    line(center + half_width, top, center, mid)
    # ---- 竖干：从中点向下 -------------------------------------------------
    line(center, mid, center, bottom)
    # ---- 两道横杠 ---------------------------------------------------------
    bar_half = half_width * 0.92
    line(center - bar_half, mid + size * 0.075, center + bar_half, mid + size * 0.075)
    line(center - bar_half, mid + size * 0.175, center + bar_half, mid + size * 0.175)


def _add_highlight(canvas: Image.Image) -> None:
    """在顶部叠加一层极淡的高光，形成"材质感"。

    强度刻意很低（6% 白）：太高会让图标看起来像塑料按钮，
    与 Apple 系统图标的克制风格不符。
    """
    size = canvas.width
    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)

    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=int(size * CORNER_RATIO),
        fill=255,
    )
    ImageDraw.Draw(highlight).ellipse(
        (-size * 0.25, -size * 0.72, size * 1.25, size * 0.42),
        fill=(255, 255, 255, 16),
    )
    canvas.paste(Image.alpha_composite(canvas, highlight), (0, 0), mask)


def build_icon(
    *,
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    accent2: tuple[int, int, int] = DEFAULT_ACCENT_2,
    size: int = 256,
) -> Image.Image:
    """生成指定边长的图标（RGBA）。"""
    work = size * SUPERSAMPLE
    canvas = _gradient_background(work, accent, accent2)
    _draw_glyph(canvas)
    _add_highlight(canvas)
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AccountBook 应用图标")
    parser.add_argument("--accent", default="#0A84FF", help="渐变起始色（十六进制）")
    parser.add_argument("--accent2", default="#5856D6", help="渐变结束色（十六进制）")
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parents[1] / "src" / "accountbook" / "resources" / "icons"),
        help="输出目录",
    )
    args = parser.parse_args()

    accent = tuple(int(args.accent.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    accent2 = tuple(int(args.accent2.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    images = [build_icon(accent=accent, accent2=accent2, size=s) for s in ICON_SIZES]  # type: ignore[arg-type]
    largest = images[-1]
    assert largest is not None

    ico_path = out_dir / "app.ico"
    # Pillow 会把 images 列表写成一个包含多尺寸的 .ico
    largest.save(ico_path, format="ICO", sizes=[(s, s) for s in ICON_SIZES])

    png_path = out_dir / "app.png"
    largest.save(png_path, format="PNG")

    print(f"[generate_icon] 已生成：{ico_path}（{len(ICON_SIZES)} 个尺寸）")
    print(f"[generate_icon] 已生成：{png_path}（256×256 预览）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
