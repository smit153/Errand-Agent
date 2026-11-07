"""BUG-006 regression: the smoke runner must read ModelMux's real secrets-file format."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "modelmux_key.sh"
FAKE_KEY = "A" * 20 + "b-_" + "9" * 20  # 43 base64url chars, like ModelMux's own keys


def _run(tmp_path: Path, content: str) -> subprocess.CompletedProcess[str]:
    secrets = tmp_path / "secrets.env"
    secrets.write_text(content, encoding="utf-8")
    return subprocess.run(["bash", str(SCRIPT), str(secrets)], capture_output=True, text=True, check=False)  # noqa: S603, S607


def test_reads_modelmux_keys_variable(tmp_path: Path) -> None:
    result = _run(tmp_path, f"MODELMUX_API_KEYS={FAKE_KEY}\n")
    assert result.returncode == 0
    assert result.stdout == FAKE_KEY


@pytest.mark.parametrize(
    "content",
    [
        f"MODELMUX_API_KEY={FAKE_KEY}\n",  # the singular name BUG-006 read
        "MODELMUX_API_KEYS=short\n",
        f"MODELMUX_API_KEYS={FAKE_KEY}\nOTHER=1\n",
        f"MODELMUX_API_KEYS='{FAKE_KEY}'\n",
        "",
    ],
)
def test_rejects_anything_else_without_echoing_it(tmp_path: Path, content: str) -> None:
    result = _run(tmp_path, content)
    assert result.returncode == 2
    assert result.stdout == ""
    assert FAKE_KEY not in result.stderr
