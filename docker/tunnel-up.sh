#!/usr/bin/env bash
#
# Bring the feed up behind its Cloudflare Tunnel.
#
#   ./docker/tunnel-up.sh
#
# Expects, next to the repo:
#   .env                             TUNNEL_ID, TUNNEL_HOSTNAME
#   docker/cloudflared/credentials.json   from `cloudflared tunnel create`
#
# Idempotent: generates the tunnel config from the template and (re)starts the
# stack. Safe to re-run after a pull.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }
ok() { printf '\033[1;32m✓\033[0m %s\n' "$1"; }

[[ -f .env ]] || fail ".env is missing. It needs TUNNEL_ID and TUNNEL_HOSTNAME."

set -a; . ./.env; set +a

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

# The cloudflared image runs as nonroot (65532), so a file owned by the
# deploying user and mode 600 is unreadable to it -- the container starts
# fine and then loops on "permission denied". Hand the file to that uid and
# keep it 600, so it stays readable by exactly one account.
CLOUDFLARED_UID=65532
if [[ "$(stat -c '%u' "$CREDS" 2>/dev/null || stat -f '%u' "$CREDS")" != "$CLOUDFLARED_UID" ]]; then
    if chown "$CLOUDFLARED_UID:$CLOUDFLARED_UID" "$CREDS" 2>/dev/null; then
        ok "credentials handed to uid $CLOUDFLARED_UID"
    elif sudo -n chown "$CLOUDFLARED_UID:$CLOUDFLARED_UID" "$CREDS" 2>/dev/null; then
        ok "credentials handed to uid $CLOUDFLARED_UID (via sudo)"
    else
        fail "Cannot change owner of $CREDS. Run:
    sudo chown $CLOUDFLARED_UID:$CLOUDFLARED_UID $CREDS
  Without it cloudflared cannot read the file and will loop on 'permission denied'."
    fi
fi

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
