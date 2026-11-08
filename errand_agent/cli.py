"""Thin terminal client for the local API (stage 8). Standard library only: runs with any
Python 3.12+ from the repository root, no install needed.

    python3 -m errand_agent.cli chat [--session N]          # chat; notifications appear as they arrive
    python3 -m errand_agent.cli trace --follow --new-only    # live trace pane (run beside the chat)
    python3 -m errand_agent.cli demo next-booking timeout_booked   # demo controls (server --demo-controls)
    python3 -m errand_agent.cli demo provider-cancel BK-000002
    python3 -m errand_agent.cli demo bookings

The client only sends text and renders what the server returns; it holds no state the server
relies on. Each message carries its own ID, so a dropped connection is retried safely (the server
replays the stored reply). Approval cards that arrive as notifications (e.g. a replacement after a
provider cancelled) are approved in their own conversation: replying "yes" switches to it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

DEFAULT_URL = "http://127.0.0.1:8765"
CONFIRM = re.compile(r"^\s*(yes|y|confirm\s+[A-Za-z0-9]{6})\s*[.!]?\s*$", re.I)
Transport = Callable[[str, str, dict[str, Any] | None, float], tuple[int, Any]]


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def urllib_transport(base_url: str) -> Transport:
    if not re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", base_url.rstrip("/")):
        raise SystemExit(f"the client only talks to a local server, not {base_url!r}")

    def send(method: str, path: str, body: dict[str, Any] | None, timeout: float) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - local http URL checked above
            base_url.rstrip("/") + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local http URL checked above
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except json.JSONDecodeError:
                return exc.code, {"detail": exc.reason}

    return send


@dataclass
class ChatClient:
    send: Transport
    out: TextIO = sys.stdout
    session_id: int | None = None
    receipt_id: str | None = None
    pending_card: tuple[int, str] | None = None  # (conversation, receipt) of a card from a notification
    sleep: Callable[[float], None] = time.sleep
    lock: threading.Lock = field(default_factory=threading.Lock)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        status, payload = self.send(method, path, body, timeout)
        if status >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise ApiError(status, str(detail))
        return payload

    def print(self, text: str) -> None:
        with self.lock:
            self.out.write(text + "\n")
            self.out.flush()

    # ------------------------------------------------------------------ chat
    def start(self, session_id: int | None) -> None:
        if session_id is None:
            session_id = int(self.call("POST", "/sessions")["session_id"])
            self.print(f"(new conversation #{session_id})")
        else:
            states = {s["session_id"]: s["state"] for s in self.call("GET", "/sessions")}
            if session_id not in states:
                raise ApiError(404, f"no conversation #{session_id}")
            self.print(f"(resumed conversation #{session_id}, {states[session_id]})")
            if states[session_id] == "awaiting_approval":  # show the card again: approval cites what you saw
                card = self.call("GET", f"/sessions/{session_id}/card")
                self.print(card["text"])
                self.receipt_id = card["receipt_id"]
        self.session_id = session_id

    def say(self, text: str, *, attempts: int = 4) -> dict[str, Any]:
        """Send one message. Retries reuse the same message ID, so the server never runs it twice."""
        if self.session_id is None:
            raise RuntimeError("no conversation")
        if CONFIRM.match(text) and self.pending_card is not None:
            card_session, card_receipt = self.pending_card
            if card_session != self.session_id:
                self.print(f"(switching to conversation #{card_session} to approve that card)")
            self.session_id, self.receipt_id = card_session, card_receipt
            self.pending_card = None
        body: dict[str, Any] = {"text": text, "client_message_id": uuid.uuid4().hex}
        if self.receipt_id:
            body["receipt_id"] = self.receipt_id
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                reply: dict[str, Any] = self.call("POST", f"/sessions/{self.session_id}/messages", body, timeout=330)
            except ApiError as exc:
                if exc.status != 409 or "different message" in exc.detail:
                    raise
                last = exc  # still being handled: wait and ask again with the same ID
            except (OSError, urllib.error.URLError) as exc:
                last = exc  # connection dropped: retry the same message ID
            else:
                if reply.get("receipt_id"):
                    self.receipt_id = reply["receipt_id"]
                for message in reply["messages"]:
                    self.print(message)
                self.print(f"[{reply['state']}]" + (" (replayed)" if reply.get("replayed") else ""))
                return reply
            self.sleep(min(2.0 * (attempt + 1), 6.0))
        raise ApiError(503, f"server unreachable or busy: {last}")

    def check_notifications(self) -> int:
        notes = self.call("GET", "/notifications")
        for n in notes:
            self.print(f"\n--- notification (conversation #{n['session_id']}) ---\n{n['text']}")
            if n.get("receipt_id"):
                self.pending_card = (int(n["session_id"]), str(n["receipt_id"]))
                self.print(f"(reply 'yes' to approve; this switches to conversation #{n['session_id']})")
        if notes:
            self.call("POST", "/notifications/ack", {"ids": [n["id"] for n in notes]})
        return len(notes)


def run_chat(client: ChatClient, session_id: int | None, stdin: TextIO = sys.stdin) -> int:
    client.start(session_id)
    client.check_notifications()
    stop = threading.Event()

    def poll() -> None:
        while not stop.wait(2.0):
            try:
                client.check_notifications()
            except (ApiError, OSError):
                continue  # the next poll tries again; nothing is lost (notifications wait until acknowledged)

    threading.Thread(target=poll, daemon=True).start()
    client.print("Type a request. Commands: /new, /session, /quit")
    try:
        for raw in stdin:
            text = raw.strip()
            if not text:
                continue
            if text == "/quit":
                break
            if text == "/new":
                client.start(None)
                client.receipt_id = None
                continue
            if text == "/session":
                client.print(f"(conversation #{client.session_id})")
                continue
            try:
                client.say(text)
            except ApiError as exc:
                client.print(f"(error: {exc.detail})")
    finally:
        stop.set()
    return 0


def latest_trace_id(client: ChatClient, path: str) -> int:
    """The newest trace line's ID (the server pages its answer, so keep asking until it's empty)."""
    after = 0
    while lines := client.call("GET", f"{path}?after={after}"):
        after = max(int(entry["id"]) for entry in lines)
    return after


def run_trace(client: ChatClient, *, session_id: int | None, follow: bool, raw: bool, new_only: bool = False) -> int:
    path = f"/sessions/{session_id}/trace" if session_id else "/trace"
    after = 0
    if new_only:
        after = latest_trace_id(client, path)
        client.print("(showing new trace lines only)")
    while True:
        lines = client.call("GET", f"{path}?after={after}" + ("&raw=true" if raw else ""))
        for entry in lines:
            client.print(json.dumps(entry, ensure_ascii=False) if raw else entry["line"])
            after = max(after, int(entry["id"]))
        if not follow:
            return 0
        client.sleep(1.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m errand_agent.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat")
    chat.add_argument("--session", type=int)
    trace = sub.add_parser("trace")
    trace.add_argument("--session", type=int)
    trace.add_argument("--follow", action="store_true")
    trace.add_argument("--raw", action="store_true")
    trace.add_argument("--new-only", action="store_true", help="skip what's already in the trace")
    sub.add_parser("sessions")
    demo = sub.add_parser("demo")
    demo_sub = demo.add_subparsers(dest="action", required=True)
    nb = demo_sub.add_parser("next-booking")
    nb.add_argument("outcome", choices=["success", "slot_taken", "timeout_booked", "timeout_lost"])
    pc = demo_sub.add_parser("provider-cancel")
    pc.add_argument("booking_id")
    demo_sub.add_parser("bookings")
    args = parser.parse_args(argv)
    client = ChatClient(urllib_transport(args.url))
    try:
        if args.command == "chat":
            return run_chat(client, args.session)
        if args.command == "trace":
            return run_trace(client, session_id=args.session, follow=args.follow, raw=args.raw, new_only=args.new_only)
        if args.command == "sessions":
            for s in client.call("GET", "/sessions"):
                client.print(f"#{s['session_id']}  {s['state']}")
            return 0
        if args.action == "next-booking":
            client.print(json.dumps(client.call("POST", "/demo/next-booking", {"outcome": args.outcome})))
        elif args.action == "provider-cancel":
            client.print(json.dumps(client.call("POST", "/demo/provider-cancel", {"booking_id": args.booking_id})))
        else:
            for b in client.call("GET", "/demo/bookings"):
                client.print(f"{b['id']}  {b['provider_id']}  {b['service']}  {b['slot']}  {b['status']}")
    except ApiError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"error: can't reach the server at {args.url} ({exc})", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
