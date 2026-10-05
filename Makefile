.PHONY: dev infra backend frontend quant rlcd rlcd-train test containers down

DATABASE_URL ?= postgres://viewengine:viewengine@localhost:5433/viewengine

# Postgres + Redis in Docker, API and UI on the host (so the API can launch your Claude Code / Codex / Copilot CLIs).
dev: infra
	$(MAKE) -j4 backend frontend quant rlcd

infra:
	docker compose up -d postgres redis

backend:
	cd backend && DATABASE_URL=$(DATABASE_URL) cargo run

frontend:
	cd frontend && npm install --silent && npm run dev

# Market data + options positioning + ML sidecar for trading desk tasks (free data sources).
quant:
	cd quant && uv run uvicorn app.main:app --host 127.0.0.1 --port 8090

# RLCD: the calibrated decision model (intent routing + fast day-trade call). Independent API on port 8095.
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
