#!/bin/bash
# Entrypoint for openhost-gemini.
#
# On every boot we:
#   1. Resolve the Gemini hostname (env var GEMINI_HOSTNAME if set,
#      else "<app_name>.<zone_domain>"). Agate uses this as its
#      --hostname flag: requests whose Host header doesn't match are
#      refused, and the TLS cert agate auto-generates is issued for
#      this name.
#   2. Make sure the content root exists and, if it is empty,
#      copy the bundled default_content/ into it. We never overwrite
#      operator edits.
#   3. Make sure the certs dir exists. Agate generates a self-signed
#      ECDSA P-256 cert for the hostname on first boot if no cert is
#      found; subsequent boots reuse the existing cert/key.
#   4. Launch the HTTP sidecar (Starlette + Uvicorn) on
#      :${STATUS_PORT:-8080} for the OpenHost router's health-check,
#      landing page, and gemtext source editor.
#   5. Launch agate in the foreground as an unprivileged user.
#   6. Supervise both children: if either exits, kill the other and
#      exit so OpenHost restarts the container.

set -euo pipefail

log() { printf '[start.sh] %s\n' "$*" >&2; }

DATA_DIR="${OPENHOST_APP_DATA_DIR:-/var/lib/openhost-gemini}"
CONTENT_DIR="$DATA_DIR/content"
CERTS_DIR="$DATA_DIR/certs"
export STATUS_PORT="${STATUS_PORT:-8080}"
DEFAULT_CONTENT_SRC="/usr/local/share/openhost-gemini/default_content"

resolve_hostname() {
    if [[ -n "${GEMINI_HOSTNAME:-}" ]]; then
        printf '%s' "$GEMINI_HOSTNAME"
        return
    fi
    if [[ -n "${OPENHOST_ZONE_DOMAIN:-}" ]]; then
        printf '%s.%s' "${OPENHOST_APP_NAME:-gemini}" "$OPENHOST_ZONE_DOMAIN"
        return
    fi
    # Nothing useful set: fall back to a placeholder so the boot log
    # makes the misconfiguration obvious instead of silently working
    # only for localhost clients.
    printf 'gemini.invalid.example'
}

HOSTNAME="$(resolve_hostname)"
export GEMINI_RESOLVED_HOSTNAME="$HOSTNAME"
log "HOSTNAME=$HOSTNAME"
log "DATA_DIR=$DATA_DIR"

# --- prepare filesystem layout ----------------------------------------
#
# Make the persistent-volume directories if they don't exist. We do
# not touch anything that is already there, so operator edits survive
# container rebuilds.
if ! mkdir -p "$DATA_DIR" "$CONTENT_DIR" "$CERTS_DIR"; then
    log "FATAL: could not create persistent dirs under $DATA_DIR"
    exit 1
fi

# Seed the content root with a default index.gmi if (and only if) the
# content dir is empty. Uses find -quit for an O(1) emptiness check and
# -mindepth 1 so the content dir itself doesn't count.
content_is_empty() {
    local probe
    if ! probe=$(find "$CONTENT_DIR" -mindepth 1 -print -quit); then
        log "WARNING: could not probe $CONTENT_DIR for emptiness; assuming it has content"
        return 1
    fi
    [[ -z "$probe" ]]
}

if content_is_empty; then
    log "content dir is empty, seeding from $DEFAULT_CONTENT_SRC"
    if ! cp -a "$DEFAULT_CONTENT_SRC/." "$CONTENT_DIR/"; then
        log "FATAL: failed to seed default content"
        exit 1
    fi
else
    log "content dir has existing content; not reseeding"
fi

# Agate's auto-cert-generation writes to $CERTS_DIR/<hostname>/. We
# let it do so rather than pre-generating with openssl: agate picks
# up the same hostname we pass via --hostname, uses ECDSA P-256 by
# default, and writes cert.pem + key.pem alongside a small state
# file. If an operator drops in their own pair they just have to
# place a cert.pem + key.pem under $CERTS_DIR/<hostname>/ and
# restart the container.
#
# We deliberately run agate (and the sidecar) as the container's
# root user rather than dropping to a separate `agate` system user.
# Under rootless podman the container "root" is already mapped to
# an unprivileged host uid, so this is not a privilege escalation;
# and it sidesteps the rootless-volume ownership problem -- the
# OpenHost-mounted persistent volume comes in owned by the host
# root, which the container "root" can read and write but a
# different in-container user cannot. (Apps that create all their
# state from scratch -- e.g. xmpp's prosody storing accounts in a
# fresh sqlite db -- can run as a non-root user; we read seeded
# .gmi files written by start.sh, so the simpler approach wins.)

# --- supervise both children ------------------------------------------
#
# Same pattern as openhost-xmpp: register the SIGTERM trap first so a
# `docker stop` during the small window between backgrounding and
# trap-install doesn't orphan the children. PID vars initialised to
# empty so `kill` with an empty arg is a no-op if we get a signal
# before backgrounding completes.
STATUS_PID=""
AGATE_PID=""
trap 'kill -TERM ${AGATE_PID:-} ${STATUS_PID:-} 2>/dev/null; wait' TERM INT

log "starting HTTP sidecar on :$STATUS_PORT"
# Launch via uvicorn so we get HTTP/1.1 keep-alive, proper graceful
# shutdown on SIGTERM, and async file IO. The sidecar reads
# OPENHOST_APP_DATA_DIR and GEMINI_RESOLVED_HOSTNAME from the
# environment we just exported.
cd /usr/local/share/openhost-gemini/sidecar
python3 -m uvicorn server:app \
    --host 0.0.0.0 \
    --port "$STATUS_PORT" \
    --log-level info \
    --no-access-log &
STATUS_PID=$!
cd - >/dev/null

log "starting agate for $HOSTNAME on :1965"
# --skip-port-check: OpenHost may publish us on a host port other
# than 1965 in the future (e.g. if an operator remaps the
# [[ports]] entry). The request URL Gemini clients send to us
# through DNS + OpenHost's publish will still carry the public
# port. With the current cloudinabottle.toml this is harmless; leaving
# the flag on keeps things robust if the mapping ever changes.
/usr/local/bin/agate \
    --hostname "$HOSTNAME" \
    --content "$CONTENT_DIR" \
    --certs "$CERTS_DIR" \
    --addr '0.0.0.0:1965' \
    --skip-port-check \
    --log-ip &
AGATE_PID=$!

# `set -e` is deliberately off around wait -n so a non-zero child
# exit doesn't abort the supervisor before we reach the kill-other
# step below.
set +e
wait -n "$AGATE_PID" "$STATUS_PID"
EXIT_CODE=$?
set -e

log "child exited (code=$EXIT_CODE); stopping container"
kill -TERM "$AGATE_PID" "$STATUS_PID" 2>/dev/null || true
wait || true
exit "$EXIT_CODE"
