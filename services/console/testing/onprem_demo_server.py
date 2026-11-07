"""Operator-local interactive demonstration. Product /v1 writes stay closed."""

import argparse
import asyncio
import json
import os
import secrets
import sys
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from onprem_response_console import LocalReader, build, canonical, private_read
from starlette.responses import JSONResponse, Response

from bravo_console import ConsoleApp

ASSETS = Path(__file__).with_name("onprem_demo_assets")
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class DemoHost(LocalReader):
    def __init__(self, manager, *, port, ttl=7200):
        origin = f"http://127.0.0.1:{port}"
        super().__init__(
            ConsoleApp(FastAPI(), public_origin=origin), port=port, plan_id="", ttl=ttl
        )
        self.manager, self.origin = manager, origin
        self.csrf = secrets.token_urlsafe(32)
        self.control = self.dispatch_control
        self.init = b"import '/__demo/client.js';"
        self.notice = '<aside role="note" class="notice">실제 온프렘 시험 모드 · 전용 무상태 HTTP 자원만 변경합니다. 기존 운영 앱·DB는 변경하지 않습니다. 통제된 설정 위반 대응이며 제품 승인·일반 공격 탐지와 구분합니다.</aside>'
        self.mounted = None

    def current(self):
        status = self.manager.status()
        if status["plan_id"] and self.mounted != status["session_id"]:
            reader = build(
                self.manager.session / "state", self.manager.settings.producer_revision, self.port
            )
            reader.cookie, reader.until = self.cookie, self.until
            self.app = reader.app
            self.mounted = status["session_id"]
        return {**status, "csrf": self.csrf, "qualification_only": True}

    async def dispatch_control(self, scope, receive, send):
        headers = scope.get("headers", [])
        pairs = dict(headers)

        async def answer(status, value):
            return await JSONResponse(value, status_code=status, headers=HEADERS)(
                scope, receive, send
            )

        if (
            any(
                sum(k == name for k, _ in headers) > 1
                for name in (b"origin", b"host", b"content-length", b"x-demo-csrf")
            )
            or pairs.get(b"origin", self.origin.encode()) != self.origin.encode()
            or pairs.get(b"sec-fetch-site", b"same-origin") not in {b"same-origin", b"none"}
        ):
            return await answer(403, {"error": "same_origin_required"})
        path, method = scope["path"], scope["method"]
        if method == "GET" and path in {"/__demo/client.js", "/__demo/style.css"}:
            name = "client.js" if path.endswith(".js") else "style.css"
            return await Response(
                (ASSETS / name).read_bytes(),
                media_type="application/javascript" if name.endswith(".js") else "text/css",
                headers=HEADERS,
            )(scope, receive, send)
        if method == "GET" and path == "/__demo/status":
            try:
                value = await asyncio.to_thread(self.current)
                return await answer(200, value)
            except Exception:
                return await answer(503, {"error": "demo_observation_unavailable"})
        if method != "POST" or path != "/__demo/action":
            return await answer(404, {"error": "not_found"})
        if (
            pairs.get(b"origin") != self.origin.encode()
            or not secrets.compare_digest(pairs.get(b"x-demo-csrf", b""), self.csrf.encode())
            or pairs.get(b"content-type") != b"application/json"
            or b"transfer-encoding" in pairs
        ):
            return await answer(403, {"error": "operator_session_required"})
        try:
            length = int(pairs.get(b"content-length", b"0"))
            if not 0 < length <= 2048:
                raise ValueError()
            raw = b""
            while True:
                message = await asyncio.wait_for(receive(), 2)
                if message["type"] != "http.request":
                    raise ValueError()
                raw += message.get("body", b"")
                if len(raw) > 2048:
                    raise ValueError()
                if not message.get("more_body"):
                    break
            if len(raw) != length:
                raise ValueError()
            value = json.loads(raw)
            accepted = await asyncio.to_thread(self.manager.act, value)
        except (ValueError, TypeError, KeyError):
            return await answer(409, {"error": "action_or_scope_not_current"})
        except Exception:
            return await answer(
                503, {"error": "demo_action_unconfirmed", "retry_automatically": False}
            )
        return await answer(202, accepted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18142)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    # The producer root is trusted operator configuration, never an HTTP input.
    config = json.loads(private_read(args.settings))
    sys.path.insert(0, str(Path(config["producer_root"]) / "demo"))
    from onprem_response.supervisor import Settings, Supervisor

    settings = Settings.read(args.settings)
    settings.validate()
    manager = Supervisor(args.state, settings)
    app = DemoHost(manager, port=args.port)
    tokens = list(app.tokens)
    access = {
        "bootstrap_url": f"http://127.0.0.1:{args.port}/__open/" + tokens[0],
        "verification_url": f"http://127.0.0.1:{args.port}/__open/" + tokens[1],
        "expires_at": (datetime.now(UTC) + timedelta(seconds=app.ttl)).isoformat(),
    }
    path = args.state / "console-access.private.json"
    path.write_bytes(canonical(access))
    path.chmod(0o600)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    timer = threading.Timer(app.ttl, lambda: setattr(server, "should_exit", True))
    timer.daemon = True
    timer.start()
    if args.open:
        opener = threading.Timer(1, lambda: webbrowser.open(access["bootstrap_url"]))
        opener.daemon = True
        opener.start()
    print(
        f"Sova personal on-prem demo console: http://127.0.0.1:{args.port}/console/ (use private opening link)",
        flush=True,
    )
    server.run()


if __name__ == "__main__":
    main()
