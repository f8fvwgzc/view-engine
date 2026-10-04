.PHONY: dev infra backend frontend quant test containers down

DATABASE_URL ?= postgres://godview:godview@localhost:5433/godview

# Postgres + Redis in Docker, API and UI on the host (so the API can launch your Claude Code / Codex / Copilot CLIs).
dev: infra
	$(MAKE) -j3 backend frontend quant

infra:
	docker compose up -d postgres redis

backend:
	cd backend && DATABASE_URL=$(DATABASE_URL) cargo run

frontend:
	cd frontend && npm install --silent && npm run dev

# Market data + options positioning + ML sidecar for trading desk tasks (free data sources).
quant:
	cd quant && uv run uvicorn app.main:app --host 127.0.0.1 --port 8090

test:
	cd backend && cargo test
	cd frontend && npm run typecheck
	cd quant && uv run pytest -q

# Everything in containers (Ollama / OpenAI-compatible / simulated providers only).
containers:
	docker compose --profile containers up --build -d

down:
	docker compose --profile containers down
