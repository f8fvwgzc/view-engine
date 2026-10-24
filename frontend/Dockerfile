# The UI, built and served by the HTTP/3 edge (Caddy), which also proxies /api and /ws to the backend.
# Build context is the repo root (see docker-compose.yml): the edge config lives in edge/.
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM caddy:2.10-alpine
COPY edge/Caddyfile edge/api.caddy /etc/caddy/
COPY --from=build /app/dist /srv
EXPOSE 8443/tcp 8443/udp
