"""`viewengine.sidecar.v1.Sidecar` server for a FastAPI sidecar.

Each `Call` is replayed into the same FastAPI app over ASGI (in-process, no socket), so the gRPC port serves
exactly the routes, validation, error handlers, auth and `Cache-Control` of the HTTP port. The server runs on the
app's event loop, started and stopped by the app's lifespan.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("view_engine_grpc")

PROTO = "viewengine/sidecar/v1/sidecar.proto"
# Snapshots and datasets are well over gRPC's 4 MB default.
MAX_MESSAGE = 64 * 1024 * 1024
# Call metadata passed on to the app as request headers (RLCD checks its API key on both transports).
FORWARD = ("authorization", "x-api-key", "x-request-id")


def proto_root() -> Path:
    """`proto/` of the repo (this file is proto/python/view_engine_grpc/bridge.py); VIEW_ENGINE_PROTO_DIR overrides."""
    override = os.environ.get("VIEW_ENGINE_PROTO_DIR")
    return Path(override) if override else Path(__file__).resolve().parents[2]


def etag_of(body: bytes) -> str:
    return 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'


def _load():
    import grpc

    root = str(proto_root())
    if root not in sys.path:
        sys.path.append(root)
    protos, services = grpc.protos_and_services(PROTO)
    return grpc, protos, services


def _servicer(app: Any, grpc: Any, protos: Any, services: Any):
    class Sidecar(services.SidecarServicer):
        def __init__(self) -> None:
            self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar.internal")

        async def Call(self, request, context):
            path = request.path or "/"
            if not path.startswith("/") or path.startswith("//"):
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"path must be a route path, got {path!r}")
            method = (request.method or "GET").upper()
            if method not in ("GET", "POST"):
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"unsupported method {method}")
            headers = {key: value for key, value in context.invocation_metadata() if key in FORWARD}
            if request.body:
                headers["content-type"] = "application/json"
            r = await self.client.request(method, path, params=[(q.key, q.value) for q in request.query],
                                          content=request.body or None, headers=headers)
            body = r.content
            cache_control = r.headers.get("cache-control", "")
            if r.status_code != 200:
                return protos.CallResponse(status=r.status_code, body=body, cache_control=cache_control,
                                           content_type=r.headers.get("content-type", ""))
            etag = r.headers.get("etag") or etag_of(body)
            if request.if_none_match and request.if_none_match == etag:
                return protos.CallResponse(status=304, cache_control=cache_control, etag=etag)
            return protos.CallResponse(status=200, body=body, cache_control=cache_control, etag=etag,
                                       content_type=r.headers.get("content-type", ""))

    return Sidecar()


async def serve_grpc(app: Any, port_env: str, default_port: int) -> Optional[Any]:
    """Starts the bridge on `$port_env` (default `default_port`; "0"/"off" disables it). Returns the server to stop
    on shutdown, or None when it is disabled or cannot start (the HTTP port keeps working either way)."""
    raw = os.environ.get(port_env, str(default_port)).strip().lower()
    if raw in ("", "0", "off", "false"):
        return None
    host = os.environ.get("SIDECAR_GRPC_BIND", "127.0.0.1")
    try:
        grpc, protos, services = _load()
        server = grpc.aio.server(compression=grpc.Compression.Gzip, options=[
            ("grpc.max_send_message_length", MAX_MESSAGE),
            ("grpc.max_receive_message_length", MAX_MESSAGE),
            # The API pings idle channels every 30 s; accept that instead of answering with GOAWAY.
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_recv_ping_interval_without_data_ms", 10_000),
        ])
        services.add_SidecarServicer_to_server(_servicer(app, grpc, protos, services), server)
        server.add_insecure_port(f"{host}:{int(raw)}")
        await server.start()
    except Exception as e:  # missing grpcio, unreadable proto, port in use: degrade to HTTP only
        log.warning("gRPC bridge not started on %s:%s (%s: %s); HTTP only", host, raw, type(e).__name__, e)
        return None
    log.warning("gRPC bridge listening on %s:%s", host, raw)
    return server
