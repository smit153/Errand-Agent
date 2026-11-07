"""Verify every hash in pip-style lock files against PyPI's official JSON metadata.

Packages may be downloaded from a mirror, but installs use --require-hashes; this check proves
each pinned hash is one PyPI itself published for that exact name and version. Standard library
only, so it runs on the host before any dependency is installed:

    python3 -I scripts/verify_lock_hashes.py requirements.lock requirements-dev.lock
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def parse_lock(path: Path) -> dict[tuple[str, str], set[str]]:
    pins: dict[tuple[str, str], set[str]] = {}
    current: tuple[str, str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _REQ.match(line)
        if match:
            current = (match.group(1).lower().replace("_", "-"), match.group(2))
            pins.setdefault(current, set())
        if current is not None:
            pins[current].update(_HASH.findall(line))
    return pins


def official_hashes(name: str, version: str, *, attempts: int = 3) -> set[str]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310 - fixed https pypi.org URL
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            data = json.loads(body)
            return {str(f["digests"]["sha256"]) for f in data["urls"]}
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
    raise RuntimeError(f"could not fetch {url} after {attempts} attempts") from last_error


def main(paths: list[str]) -> int:
    failures: list[str] = []
    checked = 0
    for path in paths:
        for (name, version), hashes in sorted(parse_lock(Path(path)).items()):
            if not hashes:
                failures.append(f"{path}: {name}=={version} has no hashes")
                continue
            unknown = hashes - official_hashes(name, version)
            if unknown:
                failures.append(f"{path}: {name}=={version} has non-PyPI hashes {sorted(unknown)}")
            checked += 1
    for failure in failures:
        print("FAIL", failure)
    print(f"checked {checked} pinned packages; {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
