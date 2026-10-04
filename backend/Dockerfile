FROM rust:1.91-bookworm AS builder
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && printf 'fn main() {}' > src/main.rs && cargo build --release
COPY src ./src
COPY migrations ./migrations
RUN touch src/main.rs && cargo build --release

FROM debian:bookworm-slim
RUN useradd --create-home appuser
COPY --from=builder /app/target/release/godview-api /usr/local/bin/godview-api
COPY skills /app/skills
USER appuser
EXPOSE 3001
CMD ["godview-api"]
