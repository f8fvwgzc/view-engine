#!/usr/bin/env bash
set -euo pipefail

transport_dir="${CODEX_TRANSPORT_DIR:-/private/tmp/godview-codex-transport}"
token_file="$transport_dir/token"
pid_file="$transport_dir/app-server.pid"
log_file="$transport_dir/app-server.log"
listen_url="${CODEX_APP_SERVER_LISTEN:-ws://0.0.0.0:4500}"

prepare_transport() {
  mkdir -p "$transport_dir"
  chmod 700 "$transport_dir"
  if [[ ! -s "$token_file" ]]; then
    umask 077
    openssl rand -hex 32 > "$token_file"
  fi
  chmod 600 "$token_file"
}

is_running() {
  [[ -s "$pid_file" ]] && kill -0 "$(<"$pid_file")" 2>/dev/null
}

case "${1:-start}" in
  start)
    prepare_transport
    if is_running; then
      echo "Codex app-server is already running (pid $(<"$pid_file"))."
      exit 0
    fi
    nohup codex app-server --listen "$listen_url" --ws-auth capability-token --ws-token-file "$token_file" > "$log_file" 2>&1 &
    echo $! > "$pid_file"
    sleep 1
    if ! is_running; then
      cat "$log_file" >&2
      exit 1
    fi
    echo "Codex app-server is listening on ${listen_url}."
    ;;
  run)
    prepare_transport
    echo "$$" > "$pid_file"
    exec codex app-server --listen "$listen_url" --ws-auth capability-token --ws-token-file "$token_file"
    ;;
  stop)
    if is_running; then
      kill "$(<"$pid_file")"
      echo "Stopped Codex app-server."
    fi
    rm -f "$pid_file"
    ;;
  status)
    if is_running; then
      echo "Codex app-server is running (pid $(<"$pid_file"))."
    else
      echo "Codex app-server is not running."
      exit 1
    fi
    ;;
  logs)
    touch "$log_file"
    tail -n 100 "$log_file"
    ;;
  *)
    echo "Usage: $0 {start|run|stop|status|logs}" >&2
    exit 2
    ;;
esac
