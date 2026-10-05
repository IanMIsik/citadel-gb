#!/usr/bin/env bash
# Pull the latest code and restart Citadel. Run as root on the instance:  sudo /opt/citadel/deploy/aws/update.sh
set -euo pipefail
APP_DIR=/opt/citadel
git -C "$APP_DIR" pull --ff-only
docker compose -p citadel --env-file "$APP_DIR/.env.aws" -f "$APP_DIR/deploy/aws/docker-compose.yml" up -d --build
docker image prune -f >/dev/null
echo "updated to $(git -C "$APP_DIR" log --oneline -1)"
