#!/usr/bin/env bash
# Redeploy PromoScout on the server: pull, rebuild (cached layers), restart, verify.
set -euo pipefail
cd "$(dirname "$0")"

# use sudo only if this user cannot talk to Docker
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

git pull --ff-only
[ -f .env ] || cp .env.example .env

# plain docker build: works without the buildx plugin that "docker compose build" needs
$DOCKER build -t promoscout:latest .
$DOCKER compose up -d          # bot stops cleanly (checkpoint saved), new container picks up the queue
$DOCKER image prune -f

sleep 10
$DOCKER compose ps
$DOCKER compose logs --tail 20 bot
$DOCKER compose exec -T bot python -m promoscout check || true
