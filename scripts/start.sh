#!/usr/bin/env bash
# Starts the whole system and tells you where to open it. Picks a free port for the console, so a clash cannot stop it,
# and shows what went wrong if something does not start.
#
#   scripts/start.sh            start
#   scripts/start.sh stop       stop
#
# Set CORE_PORT yourself to ask for a particular port. HF_CACHE reuses AI models already on this machine.
set -u
cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop or the docker service, then run this again." >&2
  exit 1
fi
if [ "${1:-}" = "stop" ]; then
  docker compose down
  exit $?
fi

in_use() {   # true when something already listens on this port, whether or not it is Docker
  if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"
  elif command -v lsof >/dev/null 2>&1; then lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
  fi
}

port="${CORE_PORT:-8000}"
while in_use "$port"; do
  echo "Port $port is taken, trying the next one."
  port=$((port + 1))
done
export CORE_PORT="$port"

echo "Starting ATMS. The first run builds the images and downloads the AI models, which takes a while."
if ! docker compose up -d --build; then
  echo >&2
  echo "Docker could not start everything. Status and the last log lines of each part follow." >&2
  docker compose ps -a >&2
  docker compose logs --tail 25 >&2
  exit 1
fi

echo "Waiting for the console to answer."
for _ in $(seq 1 60); do
  if curl -fs "http://localhost:$port/healthz" >/dev/null 2>&1; then
    echo
    echo "Open  http://localhost:$port/console/   and sign in with the token  ops-dev"
    echo "The AI models may still be loading for a minute. The CCTV page shows when they are ready."
    exit 0
  fi
  sleep 2
done
echo "The console did not answer in two minutes. Status and logs follow." >&2
docker compose ps -a >&2
docker compose logs --tail 40 core >&2
exit 1
