#!/usr/bin/env bash
#
# Bring the feed up behind its Cloudflare Tunnel.
#
#   ./docker/tunnel-up.sh
#
# Expects, next to the repo:
#   .env                             DPMP_API_KEY, TUNNEL_ID, TUNNEL_HOSTNAME
#   docker/cloudflared/credentials.json   from `cloudflared tunnel create`
#
# Idempotent: generates the tunnel config from the template and (re)starts the
# stack. Safe to re-run after a pull.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }
ok() { printf '\033[1;32m✓\033[0m %s\n' "$1"; }

[[ -f .env ]] || fail ".env is missing. It needs DPMP_API_KEY, TUNNEL_ID and TUNNEL_HOSTNAME."

set -a; . ./.env; set +a

: "${DPMP_API_KEY:?DPMP_API_KEY is not set in .env}"
: "${TUNNEL_ID:?TUNNEL_ID is not set in .env}"
: "${TUNNEL_HOSTNAME:?TUNNEL_HOSTNAME is not set in .env}"

CREDS=docker/cloudflared/credentials.json
[[ -f $CREDS ]] || fail "$CREDS is missing. Copy it from ~/.cloudflared/$TUNNEL_ID.json on the machine where you ran 'cloudflared tunnel create'."

# The credentials name the tunnel they belong to; a mismatch here produces a
# tunnel that connects fine and then serves nothing, which is hard to read
# from the logs.
CRED_ID=$(python3 -c "import json;print(json.load(open('$CREDS'))['TunnelID'])" 2>/dev/null || echo "")
[[ "$CRED_ID" == "$TUNNEL_ID" ]] || fail "TUNNEL_ID ($TUNNEL_ID) does not match the credentials file ($CRED_ID)."

chmod 600 .env "$CREDS" 2>/dev/null || true

# The cloudflared image is distroless, so substitution happens here.
sed -e "s|\${TUNNEL_ID}|$TUNNEL_ID|g" \
    -e "s|\${TUNNEL_HOSTNAME}|$TUNNEL_HOSTNAME|g" \
    docker/cloudflared/config.template.yml > docker/cloudflared/config.yml
ok "config generated for $TUNNEL_HOSTNAME"

docker compose --env-file .env -f docker/compose.tunnel.yaml up -d --build
ok "stack started"

cat <<EOF

  https://$TUNNEL_HOSTNAME

A first run with an empty volume crawls the whole timetable and routes every
shape before it answers, which takes about 7 minutes. Until then the tunnel is
up but the origin is not listening, so Cloudflare returns 502 — that is
expected, not a misconfiguration.

  docker compose --env-file .env -f docker/compose.tunnel.yaml logs -f
EOF
