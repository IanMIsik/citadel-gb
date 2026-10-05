#!/usr/bin/env bash
# Citadel on a single small EC2 instance -- design target: t4g.small (2 vCPU ARM, 2 GiB),
# Ubuntu 24.04 LTS (arm64). Paste this whole file into "User data" when launching the
# instance (after filling in the CONFIG block), or copy it to the box and run it as root.
# It is safe to run again: it only changes what is missing or different.
#
# What it does: 2 GB swap, Docker, clones the repo to /opt/citadel, writes /opt/citadel/.env.aws,
# starts Postgres + Citadel + Caddy (HTTPS and an optional password) with docker compose, and
# installs a daily database backup. Log: /var/log/citadel-bootstrap.log
set -euo pipefail
exec > >(tee -a /var/log/citadel-bootstrap.log) 2>&1
echo "=== Citadel bootstrap $(date -u +%FT%TZ) ==="

############################################ CONFIG ############################################
REPO_URL="https://github.com/IanMIsik/citadel-gb.git"
REPO_BRANCH="master"
# The repo is private: a read-only token (GitHub > Settings > Developer settings > fine-grained
# tokens > only this repo > Contents: read). Leave empty only if the repo is public.
GITHUB_TOKEN=""

# ":80" serves plain HTTP on the instance's IP. A domain name (whose DNS A record already points at
# the instance) turns on automatic HTTPS, e.g. SITE_ADDRESS="citadel.example.com".
SITE_ADDRESS=":80"
# Citadel has no login of its own. Set these, or anyone who finds the address can see the pages.
BASIC_AUTH_USER=""
BASIC_AUTH_PASSWORD=""

# Data-source keys (leave empty to run without them; see .env.template in the repo).
ENTSOE_KEY=""
IRIS_CLIENT_ID=""
IRIS_CLIENT_SECRET=""
IRIS_QUEUE_NAME=""

# Optional: also copy each daily backup to this S3 bucket (needs an instance role allowing PutObject).
BACKUP_S3_BUCKET=""
################################################################################################

APP_DIR=/opt/citadel
ENV_FILE=$APP_DIR/.env.aws
COMPOSE_DIR=$APP_DIR/deploy/aws
compose() { docker compose -p citadel --env-file "$ENV_FILE" -f "$COMPOSE_DIR/docker-compose.yml" "$@"; }

if [ "$(id -u)" -ne 0 ]; then echo "run as root"; exit 1; fi

echo "--- swap (2 GiB box: a safety net for the pandas recompute spikes)"
if ! swapon --show | grep -q '^/swapfile'; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/99-citadel.conf
sysctl -q -p /etc/sysctl.d/99-citadel.conf

echo "--- packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git curl ca-certificates openssl
systemctl enable --now docker

echo "--- code"
if [ -n "$GITHUB_TOKEN" ]; then
  git config --global credential.helper store
  echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > /root/.git-credentials
  chmod 600 /root/.git-credentials
fi
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$REPO_BRANCH"
  git -C "$APP_DIR" checkout "$REPO_BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$REPO_BRANCH"
else
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "--- settings file ($ENV_FILE)"
# The database password is generated once and kept across re-runs.
if [ -f "$ENV_FILE" ] && grep -q '^POSTGRES_PASSWORD=' "$ENV_FILE"; then
  POSTGRES_PASSWORD=$(grep '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
else
  POSTGRES_PASSWORD=$(openssl rand -hex 24)
fi
umask 077
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
SITE_ADDRESS=${SITE_ADDRESS}
ENTSOE_KEY=${ENTSOE_KEY}
IRIS_CLIENT_ID=${IRIS_CLIENT_ID}
IRIS_CLIENT_SECRET=${IRIS_CLIENT_SECRET}
IRIS_QUEUE_NAME=${IRIS_QUEUE_NAME}
REST_POLL_INTERVAL_SECONDS=5
ENVIRONMENT_LABEL=prod
FPN_OTHER_FALLBACK_ENABLED=true
DISBSAD_DISAGGREGATION_ENABLED=true
BM_STACK_ENABLED=true
PROCESS_POOL_WORKERS=2
EOF
umask 022

echo "--- Caddy (reverse proxy)"
{
  echo "{\$SITE_ADDRESS} {"
  echo "  encode gzip"
  if [ -n "$BASIC_AUTH_USER" ] && [ -n "$BASIC_AUTH_PASSWORD" ]; then
    HASH=$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$BASIC_AUTH_PASSWORD")
    echo "  basic_auth {"
    echo "    ${BASIC_AUTH_USER} ${HASH}"
    echo "  }"
  else
    echo "  # WARNING: no password set -- the whole app is open to anyone who can reach this address"
  fi
  echo "  reverse_proxy citadel:8000"
  echo "}"
} > "$COMPOSE_DIR/Caddyfile"

echo "--- build and start (first build takes a few minutes on a small instance)"
compose up -d --build
docker image prune -f >/dev/null

echo "--- daily backup"
chmod +x "$COMPOSE_DIR/backup.sh" "$COMPOSE_DIR/update.sh"
mkdir -p /etc/citadel
echo "BACKUP_S3_BUCKET=${BACKUP_S3_BUCKET}" > /etc/citadel/backup.env
if [ -n "$BACKUP_S3_BUCKET" ] && ! command -v aws >/dev/null 2>&1; then snap install aws-cli --classic || true; fi
cat > /etc/systemd/system/citadel-backup.service <<EOF
[Unit]
Description=Citadel database backup
[Service]
Type=oneshot
EnvironmentFile=/etc/citadel/backup.env
ExecStart=$COMPOSE_DIR/backup.sh
EOF
cat > /etc/systemd/system/citadel-backup.timer <<EOF
[Unit]
Description=Daily Citadel database backup
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now citadel-backup.timer

echo "--- waiting for the app to come up"
for i in $(seq 1 60); do
  if compose exec -T citadel python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)" 2>/dev/null; then
    echo "app is healthy"; break
  fi
  sleep 10
done

TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)
IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || echo "<instance public IP>")
echo
echo "=== DONE ==="
if [ "$SITE_ADDRESS" = ":80" ]; then echo "Open: http://${IP}/fpn  (also /trips /natgrid /all-plants-boalf /bm-stack /pricing-stack /fundies)"; else echo "Open: https://${SITE_ADDRESS}/fpn"; fi
echo "Update later:  sudo $COMPOSE_DIR/update.sh      Logs: docker compose -p citadel logs -f citadel"
