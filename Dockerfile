# Agate Gemini server for OpenHost.
#
# Agate is a single-binary Rust implementation of the Gemini protocol
# (gemini://, RFC "Gemini Protocol v0.16.1"). We download the prebuilt
# upstream release binary rather than building from source because the
# binary is small (~3 MiB), the release has been signed by upstream
# (see the sha256 pin below), and there is no need to drag a full Rust
# toolchain through the layer.
#
# Base image: Debian 12 slim. We need a modern glibc (agate is linked
# against glibc 2.35+), curl+ca-certs for downloading the agate
# release, python3 + Starlette/Uvicorn for the HTTP sidecar (landing,
# health-check, and gemtext source editor), and tini so SIGTERM from
# `docker stop` reaches both children via our start.sh supervisor.
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

# Pin to a specific agate release + sha256 so rebuilds are reproducible
# and a compromised upstream release tarball would fail the verify step
# rather than silently landing in the image. Bump both values together
# when updating; the checksum is printed in the release notes of each
# GitHub release.
ARG AGATE_VERSION=3.3.22
ARG AGATE_SHA256=18773fa82b70160e77a64c788647c8e252f06e9bc2cd3f1cacea2e159206b0ef

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 python3-pip tini \
 && rm -rf /var/lib/apt/lists/*

# Pin Starlette + Uvicorn versions so the editor's behaviour can't
# silently change under a transitive update. Both are small, well-known
# Python web frameworks; we use Starlette directly (not FastAPI) to
# keep the dependency footprint minimal.
#
# --break-system-packages is needed because Debian 12's pip refuses
# global installs without it (PEP 668). We install into the system
# site-packages deliberately because there is exactly one Python
# program in this container and a venv would just add unused layers.
RUN pip3 install --no-cache-dir --break-system-packages \
        "starlette==0.41.3" \
        "uvicorn[standard]==0.32.1"

RUN set -eux; \
    url="https://github.com/mbrubeck/agate/releases/download/v${AGATE_VERSION}/agate.x86_64-unknown-linux-gnu.gz"; \
    curl -fsSL "$url" -o /tmp/agate.gz; \
    echo "${AGATE_SHA256}  /tmp/agate.gz" | sha256sum -c -; \
    gunzip /tmp/agate.gz; \
    mv /tmp/agate /usr/local/bin/agate; \
    chmod 755 /usr/local/bin/agate; \
    /usr/local/bin/agate --version

# Bundled assets: the entrypoint, the HTTP sidecar (Starlette app +
# its templates and static files), and the default gemtext content
# that gets copied into $OPENHOST_APP_DATA_DIR/content/ on first boot
# only (so operator edits are never overwritten).
COPY start.sh /usr/local/bin/start.sh
COPY sidecar/ /usr/local/share/openhost-gemini/sidecar/
COPY default_content/ /usr/local/share/openhost-gemini/default_content/
RUN chmod +x /usr/local/bin/start.sh

# We run agate and the sidecar as the container's root user. Under
# rootless podman (the OpenHost runtime) "root" inside the container
# is mapped to an unprivileged host uid, so this is not a privilege
# escalation; and it sidesteps the rootless-volume ownership issue
# where the OpenHost-mounted persistent volume arrives owned by host
# root and a different in-container user cannot read it. See
# start.sh for the full rationale.

# :8080 is the HTTP landing/health/editor port (reached via the
# OpenHost router; gated by OpenHost session auth). :1965 is the
# Gemini port (published directly on the host by OpenHost via the
# [[ports]] entry in openhost.toml). We document both via EXPOSE so
# `docker inspect` reflects intent.
EXPOSE 8080 1965

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/start.sh"]
