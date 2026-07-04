#!/usr/bin/env bash
# Server side of the integration run: starts the server, lets it settle, then
# stops it cleanly and records the exit code for later verification.
#
# This script does not decide whether a run was good. That is verified later by
# verify_server.sh, which reads the outputs produced here (server.log and the
# exit-code flag).
#
# Base logic from https://github.com/MalTeeez/packscripts-auto-builds/blob/gtnh-daily/packaging/scripts/entrypoint.sh

set -euo pipefail

SERVER_DIR="${SERVER_DIR:-/home/gtnh}"
RUN_DIR="${RUN_DIR:-$SERVER_DIR}"

SERVER_LOG="$RUN_DIR/server.log"
SERVER_EXIT_FLAG="${SERVER_EXIT_FLAG:-$RUN_DIR/server.exit}"
SERVER_READY_FLAG="${SERVER_READY_FLAG:-$RUN_DIR/server.ready}"

RCON_HOST="${RCON_HOST:-localhost}"
RCON_PORT="${RCON_PORT:-25575}"
RCON_PASSWORD="${RCON_PASSWORD:-whoahaplaintextpassword}"

SERVER_JAVA_ARGS="${SERVER_JAVA_ARGS:--Xms1G -Xmx2G -Dfml.readTimeout=5 @java9args.txt -jar lwjgl3ify-forgePatches.jar nogui}"

START_TIMEOUT="${SERVER_START_TIMEOUT:-240}"
SETTLE_DURATION="${SERVER_SETTLE_DURATION:-30}"

SERVER_PID=""
TAIL_PID=""

cleanup() {
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null
  # If we still hold the JVM (e.g. an early failure), don't leak it into the runner.
  [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null && kill "$SERVER_PID" 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

run() {
  # SERVER_JAVA_ARGS can arrive as a multi-line string. Split it on whitespace
  # into a proper argv array so the launch below stays happy.
  local java_args=()
  local arg
  for arg in $SERVER_JAVA_ARGS; do
    java_args+=("$arg")
  done

  mkdir -p "$RUN_DIR"
  rm -f "$SERVER_EXIT_FLAG" "$SERVER_READY_FLAG"

  cd "$SERVER_DIR"
  sed -i 's|eula=false|eula=true|g' eula.txt
  sed -i "s|enable-rcon=false|enable-rcon=true\nrcon.password=${RCON_PASSWORD}\nrcon.port=${RCON_PORT}|" server.properties
  sed -i 's|online-mode=true|online-mode=false|' server.properties

  # Backgrounded and logged to a file so we can watch startup while the JVM runs.
  java "${java_args[@]}" > "$SERVER_LOG" 2>&1 &
  SERVER_PID=$!

  tail -n +1 -F "$SERVER_LOG" 2>/dev/null &
  TAIL_PID=$!

  local waited=0
  while [ "$waited" -lt "$START_TIMEOUT" ]; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "server exited unexpectedly during startup"
      return
    fi
    if grep -q 'Done.*For help, type' "$SERVER_LOG" 2>/dev/null; then
      echo "server started after ${waited}s"
      : > "$SERVER_READY_FLAG"
      break
    fi
    sleep 5
    waited=$((waited + 5))
  done

  if [ ! -e "$SERVER_READY_FLAG" ]; then
    echo "server did not become ready within ${START_TIMEOUT}s"
    return
  fi

  echo "waiting ${SETTLE_DURATION}s for server to settle..."
  sleep "$SETTLE_DURATION"

  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server exited during settling"
    return
  fi

  echo "stopping server via rcon"
  rcon-cli --host "$RCON_HOST" --port "$RCON_PORT" --password "$RCON_PASSWORD" stop
}

# Run the lifecycle without aborting on a server-side failure - capturing the
# exit code is the whole point, so verify_server.sh can judge the run.
run || true

# Reap the JVM (if it started) and persist its exit code for verify_server.sh.
if [ -n "$SERVER_PID" ]; then
  server_ec=0
  wait "$SERVER_PID" || server_ec=$?
  SERVER_PID=""
  echo "$server_ec" > "$SERVER_EXIT_FLAG"
  echo "server exited with code $server_ec"
fi
