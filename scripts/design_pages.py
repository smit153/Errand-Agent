"""One-page check for docs/DESIGN.md (PDF §5: design note, max 1 page; E45).

Renders the note to A4 (2 cm margins, 10 pt body, 8 pt code) with a local headless Chromium and
reports the page count. Writes artifacts/design/DESIGN.pdf. Exit 1 if it is longer than one page.

    python3 -I scripts/design_pages.py [--chrome PATH]
"""

from __future__ import annotations

import argparse
import glob
import html
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: 'DejaVu Sans', Arial, sans-serif; font-size: 10pt; line-height: 1.32; margin: 0; }
h1 { font-size: 14pt; margin: 0 0 6pt; }
p { margin: 0 0 5pt; }
pre { font-family: 'DejaVu Sans Mono', monospace; font-size: 7.6pt; line-height: 1.25; margin: 4pt 0 0;
      white-space: pre-wrap; border-left: 2pt solid #999; padding-left: 5pt; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 8.5pt; }
a { color: inherit; }
"""


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<em>\1</em>", out)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)


def to_html(markdown: str) -> str:
    parts: list[str] = []
    in_code = False
    code: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            parts.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    for line in markdown.splitlines():
        if line.startswith("```"):
            if in_code:
                parts.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
                code.clear()
            else:
                flush()
            in_code = not in_code
        elif in_code:
            code.append(line)
        elif line.startswith("# "):
            flush()
            parts.append(f"<h1>{inline(line[2:])}</h1>")
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
    flush()
    return f"<!doctype html><meta charset='utf-8'><style>{CSS}</style><body>{''.join(parts)}</body>"


def find_chrome() -> str | None:
    pattern = os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
    )
    found = sorted(glob.glob(pattern))
    return found[-1] if found else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", default=find_chrome())
    args = parser.parse_args()
    if not args.chrome:
        print("no headless Chromium found; pass --chrome PATH", file=sys.stderr)
        return 2
    out = ROOT / "artifacts" / "design" / "DESIGN.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "design.html"
        page.write_text(to_html((ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")), encoding="utf-8")
        subprocess.run(  # noqa: S603 - local binary, fixed arguments
            [
                args.chrome,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={out}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    pages = len(re.findall(rb"/Type\s*/Page(?!s)", out.read_bytes()))
    print(f"docs/DESIGN.md renders to {pages} A4 page(s): {out.relative_to(ROOT)}")
    return 0 if pages == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
