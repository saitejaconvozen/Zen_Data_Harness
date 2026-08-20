"""Check a published page for the layout faults you cannot see in source.

There is no headless browser on this host, so this reads the geometry directly
instead of rendering. It targets the specific ways a hand-authored SVG schematic
breaks — every one of which looks fine in the markup:

* **Text outside its box.** A label longer than the rect it sits in overflows
  silently; nothing errors, it just looks wrong.
* **Content outside the viewBox.** Anything past the edge is simply not drawn.
* **Overlapping boxes.** Two rects sharing space means one is drawn over the
  other and its label is unreadable.
* **A colour defined only in a theme block.** The classic unreadable-artifact
  bug: the viewer's default "system" state stamps no `data-theme`, so a token
  whose only definition sits behind `[data-theme="dark"]` never applies, and the
  page renders one theme's text on the other theme's ground.

    python scripts/check_page.py web/static/architecture.html
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from xml.etree import ElementTree


SVG_NS = "{http://www.w3.org/2000/svg}"

# Rough advance width per character at font-size 1, for the condensed and mono
# faces this page uses. Deliberately generous: the goal is catching a label that
# is obviously too wide, not typesetting.
CHAR_WIDTH = 0.56


def _f(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def check_theme_tokens(css: str) -> list[str]:
    """Every custom property used must be defined on bare :root."""
    problems: list[str] = []
    root_block = re.search(r":root\s*\{([^}]*)\}", css)
    if not root_block:
        return ["no bare :root block — the un-stamped (system) theme has no palette"]
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", root_block.group(1)))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    missing = sorted(used - defined)
    for token in missing:
        problems.append(f"{token} is used but never defined on bare :root")

    if not re.search(r"body\s*\{[^}]*background", css):
        problems.append("body has no explicit background — it will borrow the host ground")
    return problems


def check_svg(svg: ElementTree.Element, index: int) -> list[str]:
    """Geometry checks against the drawn coordinate space."""
    problems: list[str] = []
    view_box = svg.get("viewBox")
    if not view_box:
        return [f"svg[{index}] has no viewBox; it cannot scale"]
    try:
        min_x, min_y, width, height = (float(v) for v in view_box.split())
    except ValueError:
        return [f"svg[{index}] has a malformed viewBox: {view_box!r}"]

    if not svg.get("role"):
        problems.append(f"svg[{index}] has no role=img")
    if not svg.get("aria-label"):
        problems.append(f"svg[{index}] has no aria-label")

    rects = []
    for rect in svg.iter(f"{SVG_NS}rect"):
        x, y = _f(rect.get("x")), _f(rect.get("y"))
        w, h = _f(rect.get("width")), _f(rect.get("height"))
        if w <= 0 or h <= 0:
            continue
        rects.append((x, y, w, h))
        if x < min_x or y < min_y or x + w > min_x + width or y + h > min_y + height:
            problems.append(
                f"svg[{index}] rect at ({x:.0f},{y:.0f}) {w:.0f}x{h:.0f} "
                f"extends past the viewBox"
            )

    for text in svg.iter(f"{SVG_NS}text"):
        content = "".join(text.itertext()).strip()
        if not content:
            continue
        x, y = _f(text.get("x")), _f(text.get("y"))
        size = _f(text.get("font-size"), 12.0)
        anchor = text.get("text-anchor", "start")
        estimated = len(content) * size * CHAR_WIDTH
        left = x - estimated / 2 if anchor == "middle" else (
            x - estimated if anchor == "end" else x
        )
        right = left + estimated

        if y < min_y or y > min_y + height:
            problems.append(f"svg[{index}] text {content[:30]!r} sits outside the viewBox")
        if left < min_x - 2 or right > min_x + width + 2:
            problems.append(
                f"svg[{index}] text {content[:34]!r} (~{estimated:.0f}px wide) "
                f"runs past the drawing edge"
            )
            continue

        # If the label sits inside a box, it must fit that box.
        for rx, ry, rw, rh in rects:
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                if left < rx - 1 or right > rx + rw + 1:
                    problems.append(
                        f"svg[{index}] text {content[:34]!r} (~{estimated:.0f}px) "
                        f"overflows its {rw:.0f}px box"
                    )
                break

    # Overlapping boxes hide one another's labels.
    for i, (ax, ay, aw, ah) in enumerate(rects):
        for bx, by, bw, bh in rects[i + 1:]:
            overlap_x = min(ax + aw, bx + bw) - max(ax, bx)
            overlap_y = min(ay + ah, by + bh) - max(ay, by)
            if overlap_x > 4 and overlap_y > 4:
                # Containment is intentional (a panel around a group).
                contained = (
                    (ax <= bx and ay <= by and ax + aw >= bx + bw and ay + ah >= by + bh)
                    or (bx <= ax and by <= ay and bx + bw >= ax + aw and by + bh >= ay + ah)
                )
                if not contained:
                    problems.append(
                        f"svg[{index}] boxes at ({ax:.0f},{ay:.0f}) and "
                        f"({bx:.0f},{by:.0f}) overlap by "
                        f"{overlap_x:.0f}x{overlap_y:.0f}px"
                    )
    return problems


def check(path: Path) -> list[str]:
    html = path.read_text(encoding="utf-8")
    problems: list[str] = []

    styles = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    problems.extend(check_theme_tokens("\n".join(styles)))

    for index, block in enumerate(re.findall(r"<svg\b.*?</svg>", html, re.S)):
        # Strip entity-free parse: the fragments here are plain SVG.
        try:
            svg = ElementTree.fromstring(
                block if "xmlns" in block[:200]
                else block.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
            )
        except ElementTree.ParseError as exc:
            problems.append(f"svg[{index}] does not parse: {exc}")
            continue
        problems.extend(check_svg(svg, index))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    problems = check(args.path)
    if not problems:
        print(f"{args.path}: no layout faults found")
        return 0
    print(f"{args.path}: {len(problems)} issue(s)")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
