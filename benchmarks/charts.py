"""Portable SVG bar plots for benchmark results when Matplotlib is unavailable."""

from html import escape
from pathlib import Path


def bar_chart(path: Path, title: str, unit: str, series: list[tuple[str, float, float]]) -> None:
    """Draw a zero-based axis, repeat means and one sample standard deviation."""
    high = max((mean + sd for _, mean, sd in series), default=0) or 1
    left, width = 230, 410
    height = 105 + 40 * len(series)
    content = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="820" height="{height}" '
        f'viewBox="0 0 820 {height}" font-family="sans-serif" font-size="14">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="20" y="30" font-size="21">{escape(title)}</text>',
        f'<text x="20" y="54">{escape(unit)} · mean ± 1 sample SD</text>',
    ]
    for i, (label, mean, sd) in enumerate(series):
        y = 82 + i * 40
        end = left + width * mean / high
        lo = max(left, end - width * sd / high)
        hi = end + width * sd / high
        content.extend([
            f'<text x="20" y="{y + 5}">{escape(label)}</text>',
            f'<rect x="{left}" y="{y - 9}" width="{end - left:.2f}" height="18" fill="#348096"/>',
            f'<path d="M{lo:.2f} {y}H{hi:.2f} M{lo:.2f} {y-5}v10 M{hi:.2f} {y-5}v10" stroke="#17333b"/>',
            f'<text x="665" y="{y+5}">{mean:.4g} ± {sd:.3g}</text>',
        ])
    content.append("</svg>")
    path.write_text("\n".join(content), encoding="utf-8")
