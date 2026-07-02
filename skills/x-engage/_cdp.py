"""Shared Chrome DevTools Protocol helper for x-engage.

A thin wrapper over `websockets` that lets a script issue a sequence of CDP
commands over one connection and wait for each response by id. Used by
publish.py, reply.py, and fetch-comments.py.

Assumes the dedicated CDP Chrome at localhost:9222 is already running. The
caller is responsible for running `bash start-chrome-cdp.sh` first (it's
idempotent).
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import websockets

DEFAULT_CDP_PORT = 9222
DEFAULT_TAB_URL_SUBSTRING = "x.com"

# Page views are budgeted like writes (caps.yaml x.reads) — every navigation
# through this session counts. Soft dependency: if _metrics isn't importable
# (odd sys.path), navigation still works, just uncounted.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
try:
    import _metrics as _pv_metrics
except Exception:  # pragma: no cover
    _pv_metrics = None


def _count_page_view() -> None:
    if _pv_metrics is not None:
        try:
            _pv_metrics.log_page_view("x", via="cdp")
        except Exception:
            pass


def find_tab(cdp_port: int = DEFAULT_CDP_PORT,
             url_substring: str = DEFAULT_TAB_URL_SUBSTRING) -> dict:
    """Return the first page-type tab whose URL contains the substring,
    falling back to the first page-type tab if none match."""
    with urllib.request.urlopen(f"http://localhost:{cdp_port}/json", timeout=5) as r:
        tabs = json.load(r)
    pages = [t for t in tabs if t.get("type") == "page"]
    for t in pages:
        if url_substring in (t.get("url") or ""):
            return t
    if pages:
        return pages[0]
    raise SystemExit(f"no page-type tabs found at localhost:{cdp_port}")


class CDPSession:
    """One websocket connection to a Chrome tab, with sequential call()."""

    def __init__(self, ws):
        self.ws = ws
        self._next_id = 0

    async def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self._next_id += 1
        mid = self._next_id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data.get("id") == mid:
                return data

    async def navigate(self, url: str, settle_seconds: float = 5.0) -> None:
        await self.call("Page.navigate", {"url": url})
        _count_page_view()
        await asyncio.sleep(settle_seconds)

    async def eval_js(self, expression: str, await_promise: bool = True,
                      user_gesture: bool = True) -> Any:
        """Run JS and return the unwrapped value."""
        result = await self.call("Runtime.evaluate", {
            "expression": expression,
            "userGesture": user_gesture,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        return result.get("result", {}).get("result", {}).get("value")

    async def insert_text(self, text: str) -> None:
        """Place text into the focused composer. X uses DraftJS — Input.insertText
        is the only reliable path (execCommand and paste both fail silently)."""
        await self.call("Input.insertText", {"text": text})

    async def click(self, css_selector: str) -> bool:
        """Click an element by CSS selector with userGesture=true so X's React
        handlers accept it. Returns True if element was found."""
        ok = await self.eval_js(
            f"(() => {{ const el = document.querySelector({json.dumps(css_selector)}); "
            f"if (el) {{ el.click(); return true; }} return false; }})()"
        )
        return bool(ok)

    async def focus(self, css_selector: str) -> bool:
        ok = await self.eval_js(
            f"(() => {{ const el = document.querySelector({json.dumps(css_selector)}); "
            f"if (el) {{ el.focus(); return true; }} return false; }})()"
        )
        return bool(ok)


@asynccontextmanager
async def open_session(cdp_port: int = DEFAULT_CDP_PORT,
                       tab_url_substring: str = DEFAULT_TAB_URL_SUBSTRING):
    """Async context manager that yields a CDPSession bound to the picked tab."""
    tab = find_tab(cdp_port, tab_url_substring)
    # open_timeout caps the connect handshake so a wedged tab can't hang a cron
    # tick forever (the prior version had no timeout — an unresponsive debugger
    # socket would block indefinitely). close_timeout bounds teardown likewise.
    async with websockets.connect(
        tab["webSocketDebuggerUrl"],
        max_size=20_000_000,
        open_timeout=15,
        close_timeout=5,
    ) as ws:
        yield CDPSession(ws)
