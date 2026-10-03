.PHONY: up up-ollama up-host-codex down-host-codex codex-status codex-logs

up:
	docker compose up --build

up-ollama:
	docker compose up --build -d

up-host-codex:
	./scripts/host-codex-app-server.sh start
	AGENT_PROVIDER=codex CODEX_APP_SERVER_URL=ws://host.docker.internal:4500 CODEX_TRANSPORT_VOLUME=/private/tmp/godview-codex-transport CODEX_WORKSPACE=$(CURDIR) docker compose up --build -d

down-host-codex:
	CODEX_TRANSPORT_VOLUME=/private/tmp/godview-codex-transport docker compose down
	./scripts/host-codex-app-server.sh stop

codex-status:
	./scripts/host-codex-app-server.sh status

codex-logs:
	./scripts/host-codex-app-server.sh logs
