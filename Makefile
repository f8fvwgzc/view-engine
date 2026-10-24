.PHONY: dev infra backend frontend quant rlcd rlcd-train test containers down edge-trust

DATABASE_URL ?= postgres://viewengine:viewengine@localhost:5433/viewengine
# The HTTP/3 edge in front of the UI and API: https://localhost:$(EDGE_PORT)
EDGE_PORT ?= 8443
EDGE_SERVICE ?= edge

# Postgres + Redis + the HTTP/3 edge in Docker, API and UI on the host (so the API can launch your Claude Code /
# Codex / Copilot CLIs). Open https://localhost:$(EDGE_PORT) (or http://localhost:5173 without the edge).
dev: infra
	$(MAKE) -j4 backend frontend quant rlcd

infra:
	EDGE_PORT=$(EDGE_PORT) docker compose up -d postgres redis edge

# Trust the edge's local certificate authority (macOS keychain) so browsers accept https://localhost:$(EDGE_PORT).
# Chrome still uses HTTP/3 only for publicly trusted certificates unless started with
# --origin-to-force-quic-on=localhost:$(EDGE_PORT); Firefox needs network.http.http3.disable_when_third_party_roots_found=false.
edge-trust:
	docker compose cp $(EDGE_SERVICE):/data/caddy/pki/authorities/local/root.crt .edge-root.crt
	sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain .edge-root.crt

backend:
	cd backend && DATABASE_URL=$(DATABASE_URL) cargo run

frontend:
	cd frontend && npm install --silent && npm run dev

# Market data + options positioning + ML sidecar for trading desk tasks (free data sources).
# HTTP on 8090; the API talks to it over gRPC on 9090 (QUANT_GRPC_PORT), served by the same process.
quant:
	cd quant && uv run uvicorn app.main:app --host 127.0.0.1 --port 8090

# RLCD: the calibrated decision model (intent routing + fast day-trade call). Independent API on port 8095,
# gRPC for the View Engine API on 9095 (RLCD_GRPC_PORT).
rlcd:
	cd backend/rlcd && uv run uvicorn rlcd.api:app --host 127.0.0.1 --port 8095

# Retrain every RLCD head from the quant sidecar's history and the feedback store (needs `make quant` and `make rlcd` running).
rlcd-train:
	curl -s -X POST http://127.0.0.1:8095/v1/train -H 'content-type: application/json' -d '{}'

test:
	cd backend && cargo test
	cd frontend && npm run typecheck
	cd quant && uv run pytest -q
	cd backend/rlcd && uv run pytest -q

# Everything in containers (Ollama / OpenAI-compatible / simulated providers only).
containers:
	docker compose --profile containers up --build -d

down:
	docker compose --profile containers down
