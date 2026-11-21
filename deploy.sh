#!/usr/bin/env bash
# Redeploy PromoScout on the server: pull, rebuild (cached layers), restart, verify.
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
[ -f .env ] || cp .env.example .env

docker compose build
docker compose up -d          # bot stops cleanly (checkpoint saved), new container picks up the queue
docker image prune -f

sleep 10
docker compose ps
docker compose logs --tail 20 bot
docker compose exec -T bot python -m promoscout check || true
