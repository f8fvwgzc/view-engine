# Build context is the repo root (see docker-compose.yml): the gRPC contract lives in proto/.
FROM rust:1.91-bookworm AS builder
WORKDIR /app/backend
COPY proto /app/proto
COPY backend/Cargo.toml backend/Cargo.lock backend/build.rs ./
RUN mkdir src && printf 'fn main() {}' > src/main.rs && cargo build --release
COPY backend/src ./src
COPY backend/migrations ./migrations
RUN touch src/main.rs && cargo build --release

FROM debian:bookworm-slim
RUN useradd --create-home appuser
COPY --from=builder /app/backend/target/release/view-engine-api /usr/local/bin/view-engine-api
USER appuser
EXPOSE 3001
CMD ["view-engine-api"]
