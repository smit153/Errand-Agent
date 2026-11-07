"""Local-only network endpoints (PDF §1; DECISIONS §§10, 17, 18).

Every runtime HTTP dependency (the ModelMux gateway, the Ollama embedder) must be plain http on
this machine: a loopback address, or a bare single-label service name on the local Compose
network. Anything with a dot (a public or LAN hostname), https, or credentials in the URL is
refused before any request is made.
"""

from __future__ import annotations

from urllib.parse import urlparse

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class LocalOnlyError(ValueError):
    pass


def check_local(url: str, *, what: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "http":
        raise LocalOnlyError(f"{what} URL must be plain http on the local machine, got {url!r}")
    if parsed.username or parsed.password:
        raise LocalOnlyError(f"{what} URL must not carry credentials")
    if host not in _LOOPBACK and ("." in host or ":" in host or not host):
        raise LocalOnlyError(f"{what} host {host!r} is not loopback or a local service name")
