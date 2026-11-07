"""Export a Claude Code session transcript as readable Markdown (PDF §5: transcript of the AI
coding assistant used while building).

Keeps the conversation only: user messages, assistant text and a one-line summary of each tool
call. Drops system/API/attachment records. Redacts email addresses and account/organization IDs.
The output goes to artifacts/transcript/ (git-ignored): review it before sharing.

    python3 -I scripts/export_transcript.py ~/.claude-work/projects/<project>/<session>.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRETISH = re.compile(r"(?i)(api[_-]?key|secret|password|token)(\"?\s*[:=]\s*\"?)([^\s\"']{12,})")


def redact(text: str, ids: set[str]) -> str:
    text = EMAIL.sub("[email]", text)
    text = SECRETISH.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    for value in ids:
        text = text.replace(value, "[id]")
    return text


def tool_line(block: dict[str, Any]) -> str:
    inp = block.get("input") or {}
    detail = inp.get("description") or inp.get("file_path") or inp.get("command") or inp.get("query") or ""
    detail = " ".join(str(detail).split())
    return f"- `{block.get('name')}`: {detail[:160]}"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    source = Path(sys.argv[1]).expanduser()
    records = []
    ids: set[str] = set()
    for raw in source.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "session-owner":
            ids |= {str(rec.get("accountUuid") or ""), str(rec.get("organizationUuid") or "")} - {""}
        records.append(rec)
    out = ["# Claude Code session transcript", "", f"Source: `{source.name}` (exported; redacted).", ""]
    for rec in records:
        kind = rec.get("type")
        msg = rec.get("message") or {}
        content = msg.get("content")
        if kind == "user" and not rec.get("isMeta"):
            texts = (
                [content]
                if isinstance(content, str)
                else [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            )
            texts = [t for t in texts if t and not t.lstrip().startswith("<")]  # skip harness-injected blocks
            if texts:
                out += ["## User", "", *[redact(t, ids) for t in texts], ""]
        elif kind == "assistant" and isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text" and b.get("text", "").strip()]
            tools = [tool_line(b) for b in content if b.get("type") == "tool_use"]
            if texts:
                out += ["## Assistant", "", *[redact(t, ids) for t in texts], ""]
            if tools:
                out += [redact(t, ids) for t in tools] + [""]
    target = ROOT / "artifacts" / "transcript" / f"{source.stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {target.relative_to(ROOT)} ({target.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
