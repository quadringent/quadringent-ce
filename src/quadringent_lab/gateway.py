"""Laboratory ASGI gateway: one origin for the UI, the v2 API and the lab router.

Routing:

- ``/lab/**``        lab-only router (scenario control); never mounted by the product
- ``/lab-other/v2/**`` second organization's API (prefix stripped)
- ``/v2/**``         main organization's API (real ``create_v2_app``)
- ``/v1/**``         explicit 404: the laboratory serves the v2 surface only
- anything else      the built frontend (``ui/dist``), same origin

Requests whose ``Host`` is not loopback are refused (DNS rebinding guard).
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from .faults import apply_fault
from .router import build_admin_app
from .runtime import LabRuntime

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
OTHER_PREFIX = "/lab-other"


def _host_name(scope: dict) -> str:
    for name, value in scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1")
            if host.startswith("["):
                return host.split("]")[0] + "]"
            return host.split(":")[0]
    return ""


async def _json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
        }
    )
    await send({"type": "http.response.body", "body": body})


class LabGateway:
    def __init__(self, runtime: LabRuntime) -> None:
        self.runtime = runtime
        self.admin = build_admin_app(runtime)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if _host_name(scope) not in ALLOWED_HOSTS:
            await _json(send, 421, {"error": {"code": "wrong_environment", "message": "Loopback host required."}})
            return
        path: str = scope["path"]
        method: str = scope["method"]
        if path == "/lab" or path.startswith("/lab/"):
            await self.admin(scope, receive, send)
            return
        if path.startswith(OTHER_PREFIX + "/"):
            inner = dict(scope)
            inner["path"] = path[len(OTHER_PREFIX):]
            if "raw_path" in scope and scope["raw_path"]:
                inner["raw_path"] = scope["raw_path"][len(OTHER_PREFIX.encode()):]
            await self._forward("lab-other", inner, receive, send, method)
            return
        if path.startswith("/v2/") or path == "/llms.txt":
            await self._forward("lab", scope, receive, send, method)
            return
        if path.startswith("/v1/") or path == "/v1":
            await _json(
                send,
                404,
                {"error": {"code": "not_found", "message": "The laboratory serves the /v2 surface only."}},
            )
            return
        await self._static(path, send)

    async def _forward(self, org: str, scope: dict, receive, send, method: str) -> None:
        app = self.runtime.apps["lab" if org == "lab" else "lab-other"]
        rule = self.runtime.faults.take(org, method, scope["path"])
        if rule is None:
            await app(scope, receive, send)
        else:
            await apply_fault(rule, app, scope, receive, send)

    async def _static(self, path: str, send) -> None:
        dist = self.runtime.ui_dist
        if dist is None or not (dist / "index.html").is_file():
            await _json(
                send,
                503,
                {
                    "error": {
                        "code": "store_unavailable",
                        "message": "ui/dist is missing: run `npm --prefix ui run build` first.",
                    }
                },
            )
            return
        root = dist.resolve()
        relative = path.lstrip("/") or "index.html"
        target = (root / relative).resolve()
        if root not in target.parents and target != root:
            await _json(send, 404, {"error": {"code": "not_found", "message": "Not found."}})
            return
        if not target.is_file():
            await _json(send, 404, {"error": {"code": "not_found", "message": "Not found."}})
            return
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = [
            (b"content-type", content_type.encode("ascii")),
            (b"content-length", str(len(content)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": content})


def default_ui_dist() -> Path:
    return Path(__file__).resolve().parents[2] / "ui" / "dist"
