"""Source-checkout-only reader for the operator-owned on-prem qualification.

No Kubernetes/AWS credential, write route, workflow approval or fixture result.
The local T10 store registers the actual trial references and remains COLLECT.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

import uvicorn
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore
from starlette.responses import HTMLResponse, RedirectResponse, Response

from bravo_console import ConsoleApp
from bravo_console.workload import PlanWorkload, RegisteredWorkload

PRODUCER = "owned-onprem-qualification"


def canonical(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()


def private_read(path, limit=120000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("owner_private_regular_file_required")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("bounded_private_document_required")
        return raw
    finally:
        os.close(fd)


class Feed:
    def __init__(self, root, revision, binding):
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("pinned_producer_source_required")
        self.root, self.revision, self.binding = root, revision, binding
        self.key = private_read(root / "feed-key.private", 32)
        if len(self.key) != 32:
            raise ValueError("invalid_private_feed_key")
        self.signatures = OrderedDict()
        self.lock = threading.Lock()

    def load(self, _scope, plan):
        envelope = json.loads(private_read(self.root / "feed.private.json"))
        if set(envelope) != {"producer_id", "source_revision", "body", "hmac_sha256"}:
            raise ValueError("invalid_registered_envelope")
        if envelope["producer_id"] != PRODUCER or envelope["source_revision"] != self.revision:
            raise ValueError("unregistered_producer")
        body = envelope["body"]
        if (body["plan_id"], body["plan_digest"], body["plan_version"]) != (
            plan["plan_id"],
            self.binding["plan_digest"],
            1,
        ):
            raise ValueError("foreign_trial_plan")
        document = canonical(body)
        digest = hashlib.sha256(document).hexdigest()
        signature = envelope["hmac_sha256"]
        with self.lock:
            self.signatures[digest] = signature
            while len(self.signatures) > 8:
                self.signatures.popitem(last=False)
        value = RegisteredWorkload(PRODUCER, self.revision, digest, document)
        if not self.authenticate(_scope, plan, value):
            raise ValueError("unverified_trial_feed")
        return value

    def authenticate(self, _scope, _plan, value):
        if private_read(self.root / "feed-key.private", 32) != self.key:
            return False
        with self.lock:
            signature = self.signatures.get(value.content_sha256)
        if not isinstance(signature, str):
            return False
        envelope = {
            "producer_id": PRODUCER,
            "source_revision": self.revision,
            "body": json.loads(value.document),
        }
        expected = hmac.new(self.key, canonical(envelope), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


class LocalReader:
    def __init__(self, app, *, port, plan_id, ttl=7200):
        self.app, self.port, self.plan_id = app, port, plan_id
        self.cookie = secrets.token_urlsafe(32)
        self.tokens = {secrets.token_urlsafe(32) for _ in range(2)}
        self.until = time.monotonic() + ttl
        self.ttl = ttl
        self.control = None
        self.notice = None
        self.init = (
            b"let n=0;const timer=setInterval(()=>{"
            b"const form=document.getElementById('connect-form');"
            b"const b=document.querySelector('[data-view=topology]');"
            b"if(b&&typeof form?.onsubmit==='function'){b.click();clearInterval(timer);}"
            b"else if(++n>100)clearInterval(timer);},100);"
        )

    def authenticated(self, request):
        value = request.cookies.get("sova-onprem-trial-reader", "")
        return time.monotonic() < self.until and secrets.compare_digest(value, self.cookie)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await send({"type": "websocket.close", "code": 4403})
        headers = dict(scope.get("headers", []))
        path = scope.get("path", "")

        async def deny():
            return await HTMLResponse(
                "개인 시험 조회 접속 확인이 필요합니다.",
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)

        if (
            headers.get(b"host") != f"127.0.0.1:{self.port}".encode()
            or time.monotonic() >= self.until
        ):
            return await deny()
        token = path.removeprefix("/__open/")
        if scope["method"] == "GET" and path.startswith("/__open/") and token in self.tokens:
            self.tokens.remove(token)
            response = RedirectResponse("/console/?plan=" + self.plan_id, status_code=303)
            response.set_cookie(
                "sova-onprem-trial-reader",
                self.cookie,
                httponly=True,
                samesite="strict",
                max_age=self.ttl,
            )
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            return await response(scope, receive, send)
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get(b"cookie", b"").decode())
        except Exception:
            return await deny()
        item = cookies.get("sova-onprem-trial-reader")
        if item is None or not secrets.compare_digest(item.value, self.cookie):
            return await deny()
        if path.startswith("/__demo/") and self.control is not None:
            return await self.control(scope, receive, send)
        if scope["method"] not in {"GET", "HEAD"}:
            return await deny()
        if path == "/__trial_init.js":
            return await Response(
                self.init,
                media_type="application/javascript",
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
        if path != "/console/":
            return await self.app(scope, receive, send)
        start = None

        async def wrap(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = dict(message)
            elif message["type"] == "http.response.body":
                if message.get("more_body"):
                    raise ValueError("bounded_console_html_required")
                notice = (
                    self.notice
                    or '<aside role="note" class="notice">실제 온프렘 전용 시험 · 동일 클러스터의 새 namespace에서 HTTP 격리·대체·전환을 확인합니다. 일반 공격 탐지·운영 앱 이전·T10 제품 승인을 뜻하지 않습니다. 시험 종료·근거 만료 뒤에는 과거 기록으로 표시됩니다.</aside>'
                )
                body = re.sub(
                    rb"(<main\b[^>]*>)",
                    lambda m: m.group(0) + notice.encode(),
                    message.get("body", b""),
                    count=1,
                )
                body = body.replace(
                    b"</body>", b'<script type="module" src="/__trial_init.js"></script></body>'
                )
                start["headers"] = [
                    (k, v) for k, v in start["headers"] if k.lower() != b"content-length"
                ] + [(b"content-length", str(len(body)).encode())]
                await send(start)
                await send(dict(message, body=body))
            else:
                await send(message)

        return await self.app(scope, receive, wrap)


def build(root, revision, port):
    binding = json.loads(private_read(root / "registration.private.json"))
    metadata = json.loads(private_read(root / "trial.private.json"))
    if metadata["source_revision"] != revision or metadata["qualification_only"] is not True:
        raise ValueError("trial_source_or_scope_mismatch")
    feed = Feed(root, revision, binding)
    store = PlanStore(root / "console.private.sqlite")
    spec = PlanSpec(
        binding["plan_id"],
        binding["tenant_id"],
        binding["application_id"],
        binding["source_environment_id"],
        binding["target_environment_id"],
        binding["plan_digest"],
        binding["plan_digest"],
    )
    store.register_plan(
        spec, run_id="owned-trial-registration", occurred_at=datetime.now(UTC).isoformat()
    )
    holder = {}
    api = create_plan_app(
        store,
        resolve_principal=lambda request: (
            Principal("personal-trial-reader", binding["tenant_id"])
            if holder["reader"].authenticated(request)
            else None
        ),
    )
    workload = PlanWorkload(
        api,
        load=feed.load,
        authenticate=feed.authenticate,
        authorize=lambda _scope, plan: plan["plan_id"] == binding["plan_id"],
        producer_id=PRODUCER,
        source_revision=revision,
        provenance="live",
    )
    app = ConsoleApp(api, public_origin=f"http://127.0.0.1:{port}", workload=workload)
    reader = LocalReader(app, port=port, plan_id=binding["plan_id"])
    holder["reader"] = reader
    return reader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--producer-revision", required=True)
    parser.add_argument("--port", type=int, default=18136)
    args = parser.parse_args()
    os.umask(0o077)
    if args.state.is_symlink() or args.state.stat().st_mode & 0o077:
        parser.error("owner-private trial directory required")
    app = build(args.state, args.producer_revision, args.port)
    tokens = list(app.tokens)
    access = {
        "bootstrap_url": f"http://127.0.0.1:{args.port}/__open/" + tokens[0],
        "verification_url": f"http://127.0.0.1:{args.port}/__open/" + tokens[1],
        "expires_at": (datetime.now(UTC) + timedelta(seconds=app.ttl)).isoformat(),
        "producer_revision": args.producer_revision,
        "read_only": True,
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
    server.run()


if __name__ == "__main__":
    main()
