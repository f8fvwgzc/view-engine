FROM node:22-bookworm-slim

RUN npm install --global @openai/codex

WORKDIR /workspace
EXPOSE 4500
CMD ["codex", "app-server", "--listen", "ws://0.0.0.0:4500"]
