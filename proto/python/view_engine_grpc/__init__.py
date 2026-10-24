"""gRPC bridge and cache policy shared by the View Engine Python sidecars (quant, RLCD).

- `policy` — per-route freshness rules, sent as `Cache-Control` on 200 answers over HTTP and gRPC alike
- `bridge` — `viewengine.sidecar.v1.Sidecar/Call` server that replays each call into the sidecar's FastAPI app

The .proto lives in `proto/viewengine/sidecar/v1/` (single source for the Rust client and this server); it is
loaded at runtime with `grpc.protos_and_services`, so there are no generated files to keep in sync.
"""
from .bridge import serve_grpc
from .policy import Rule, install_cache_control

__all__ = ["Rule", "install_cache_control", "serve_grpc"]
