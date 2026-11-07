"""Loopback-only, authenticated demo wrapper. Never mounted by compose_console."""

import argparse
import asyncio
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

import uvicorn
from bravo_platform_events.plan_api import create_plan_app
from bravo_platform_events.store import EventError
from starlette.responses import JSONResponse, RedirectResponse, Response

from bravo_console import ConsoleApp
from bravo_console.app import HEADERS
from bravo_console.reports import PlanControlReport, RegisteredGateDocument

from .runtime import APPROVER, DemoWorkflow

ASSETS = Path(__file__).parent
COOKIE_NAME = "sova-demo-workflow"


class DemoApp:
    def __init__(self, runtime, *, port):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("unprivileged_local_port_required")
        self.runtime = runtime
        self.origin = f"http://127.0.0.1:{port}"
        self.host = f"127.0.0.1:{port}".encode()
        self.cookie = secrets.token_urlsafe(32)
        self.bootstrap = secrets.token_urlsafe(32)
        self.bootstrap_used = False
        self.api = create_plan_app(runtime.store, resolve_principal=lambda _: APPROVER)

        async def load(_scope, plan):
            report = runtime.saved("gate")
            if report is None:
                return None
            return RegisteredGateDocument(
                plan["plan_id"], plan["plan_digest"], json.dumps(report).encode()
            )

        async def fixture_authority(*_):
            # Explicit local synthetic authority; not a T7 identity or T19 signature.
            return True

        self.console = ConsoleApp(
            self.api,
            public_origin=self.origin,
            controls=PlanControlReport(self.api, load=load, authenticate=fixture_authority),
        )

    def response(self, body, status=200):
        return JSONResponse(body, status_code=status, headers=HEADERS)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.console(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 4403})
        headers = scope.get("headers", [])

        def values(name):
            return [v for k, v in headers if k.lower() == name]

        path, method = scope.get("path", ""), scope.get("method", "")
        expired = self.runtime.now() >= datetime.fromisoformat(self.runtime.settings["expires_at"])
        if values(b"host") != [self.host] or expired:
            return await self.response({"error": "demo_expired_or_wrong_host"}, 403)(
                scope, receive, send
            )
        if path == "/__demo/open/" + self.bootstrap and method == "GET" and not self.bootstrap_used:
            self.bootstrap_used = True
            response = RedirectResponse("/console/", status_code=303, headers=HEADERS)
            response.set_cookie(
                COOKIE_NAME, self.cookie, httponly=True, samesite="strict", max_age=7200, path="/"
            )
            return await response(scope, receive, send)
        cookie = SimpleCookie()
        try:
            cookie.load(b";".join(values(b"cookie")).decode())
            value = cookie.get(COOKIE_NAME)
            authorized = value is not None and secrets.compare_digest(value.value, self.cookie)
        except (CookieError, UnicodeError, ValueError):
            authorized = False
        if not authorized:
            return await self.response({"error": "demo_session_required"}, 403)(
                scope, receive, send
            )
        if path == "/__demo/status" and method == "GET":
            body = await asyncio.to_thread(self.runtime.view)
            return await self.response(body)(scope, receive, send)
        if path == "/__demo/action" and method == "POST":
            if (
                values(b"origin") != [self.origin.encode()]
                or values(b"x-sova-demo") != [b"1"]
                or values(b"content-type") != [b"application/json"]
                or values(b"sec-fetch-site") not in ([], [b"same-origin"])
            ):
                return await self.response({"error": "demo_origin_required"}, 403)(
                    scope, receive, send
                )
            raw = b""
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                raw += message.get("body", b"")
                if len(raw) > 512:
                    return await self.response({"error": "demo_request_too_large"}, 413)(
                        scope, receive, send
                    )
                if not message.get("more_body"):
                    break
            try:
                from bravo_gate.contract import decode

                body = decode(raw)
                if set(body) != {"action", "expected_version"} or type(body["action"]) is not str:
                    raise ValueError()
                result = await asyncio.to_thread(
                    self.runtime.action,
                    body["action"],
                    expected_version=body["expected_version"],
                )
            except EventError as error:
                return await self.response({"error": error.code}, 409)(scope, receive, send)
            except ValueError:
                return await self.response({"error": "invalid_demo_request"}, 422)(
                    scope, receive, send
                )
            return await self.response(result)(scope, receive, send)
        if method not in ("GET", "HEAD"):
            return await self.response({"error": "use_explicit_demo_controls"}, 405)(
                scope, receive, send
            )
        if path in ("/__demo/workflow.js", "/__demo/workflow.css"):
            name = "workflow.js" if path.endswith(".js") else "workflow.css"
            return await Response(
                (ASSETS / name).read_bytes(),
                headers=HEADERS,
                media_type="text/javascript" if name.endswith(".js") else "text/css",
            )(scope, receive, send)
        if path != "/console/" or method != "GET":
            return await self.console(scope, receive, send)
        start = None

        async def inject(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = dict(message)
            elif message["type"] == "http.response.body":
                if message.get("more_body"):
                    raise RuntimeError("bounded_demo_html_required")
                body = message.get("body", b"")
                panel = (ASSETS / "panel.html").read_bytes()
                body, count = re.subn(rb"(<main\b[^>]*>)", lambda m: m[0] + panel, body, count=1)
                if count != 1:
                    raise RuntimeError("demo_console_layout_changed")
                body = body.replace(
                    b"</head>", b'<link rel="stylesheet" href="/__demo/workflow.css"></head>'
                )
                body = body.replace(
                    b"</body>", b'<script type="module" src="/__demo/workflow.js"></script></body>'
                )
                start["headers"] = [
                    (k, v) for k, v in start["headers"] if k.lower() != b"content-length"
                ]
                start["headers"].append((b"content-length", str(len(body)).encode()))
                await send(start)
                await send(dict(message, body=body))

        return await self.console(scope, receive, inject)


def main():
    parser = argparse.ArgumentParser(description="Sova isolated local workflow demonstration")
    parser.add_argument("--port", type=int, default=18131)
    parser.add_argument(
        "--scenario", choices=("happy", "gate-block", "response-loss"), default="happy"
    )
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--new", type=Path, help="Create a demo only in a new or empty directory")
    storage.add_argument(
        "--resume", type=Path, help="Resume only a directory previously created by this demo"
    )
    args = parser.parse_args()
    os.umask(0o077)
    if args.resume and args.scenario != "happy":
        parser.error("--resume keeps its original scenario")
    root = args.resume or args.new or Path(tempfile.mkdtemp(prefix="sova-demo-workflow-"))
    runtime = (
        DemoWorkflow(root) if args.resume else DemoWorkflow.create(root, scenario=args.scenario)
    )
    app = DemoApp(runtime, port=args.port)
    access = root / "access.private.json"
    access.write_text(
        json.dumps(
            {
                "bootstrap_url": app.origin + "/__demo/open/" + app.bootstrap,
                "expires_at": runtime.settings["expires_at"],
                "plan_id": runtime.plan_id,
                "mode": "demo",
                "synthetic": True,
            }
        )
    )
    access.chmod(0o600)
    remaining = (
        datetime.fromisoformat(runtime.settings["expires_at"]) - datetime.now(UTC)
    ).total_seconds()
    if remaining <= 0:
        parser.error("This demo has expired. Start a new isolated demo.")
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="warning",
            timeout_graceful_shutdown=5,
        )
    )
    timer = threading.Timer(remaining, lambda: setattr(server, "should_exit", True))
    timer.daemon = True
    timer.start()
    print(
        f"DEMO ONLY · synthetic results · no production execution. Access file: {access}",
        flush=True,
    )
    try:
        server.run()
    finally:
        timer.cancel()


if __name__ == "__main__":
    main()
