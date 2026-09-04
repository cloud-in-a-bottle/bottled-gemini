#!/usr/bin/env python3
"""HTTP sidecar for the openhost-gemini app.

Three jobs:

1. Health check at ``/healthz`` (probes agate on 127.0.0.1:1965).
2. Public landing page at ``/`` describing how to point a Gemini client at
   the capsule.
3. Source editor for the capsule's ``.gmi`` files at ``/edit``, with
   a small JSON file API at ``/api/files`` and ``/api/files/<path>``.
   Edits land in ``$OPENHOST_APP_DATA_DIR/content/`` directly; agate
   re-reads files on the next request, so changes are live without a
   restart.

Run as ``start.sh`` does in the container::

    cd sidecar
    python3 -m uvicorn server:app --host 0.0.0.0 --port "$STATUS_PORT"

Outside the container, install ``sidecar/requirements.txt`` first.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.responses import PlainTextResponse
from starlette.responses import Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from feed import FeedSignature
from feed import refresh_feed

logger = logging.getLogger("openhost-gemini.sidecar")


# ----------------------------------------------------------------- config

# Agate's listen port. Used by the health probe.
AGATE_HOST = "127.0.0.1"
AGATE_PORT = 1965
PROBE_TIMEOUT_SECONDS = 1.0
FEED_REFRESH_SECONDS = 5.0

# Paths derived from the runtime environment. The defaults are used by
# the unit/dev modes; under OpenHost both env vars are always set.
DATA_DIR = Path(os.environ.get("OPENHOST_APP_DATA_DIR", "/var/lib/openhost-gemini"))
CONTENT_DIR = (DATA_DIR / "content").resolve()

# Static assets shipped in the image.
SIDECAR_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = SIDECAR_ROOT / "templates"
STATIC_DIR = SIDECAR_ROOT / "static"

# Cap saved files to a generous-but-bounded size. Gemtext is hand-edited
# prose; nobody legitimately writes a 10-MB capsule page through the
# source editor. The bound prevents a confused or hostile editor JS
# call from filling the persistent volume.
MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB

# Gemini hostname (resolved by start.sh; falls back if missing).
GEMINI_HOSTNAME = os.environ.get("GEMINI_RESOLVED_HOSTNAME", "").strip() or "your-openhost-zone"

# Permissive but safe hostname shape (RFC-ish). Anything that doesn't
# match goes through as a placeholder rather than reaching the HTML.
_VALID_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)

# Filenames stored in the content dir must look like a relative gemtext
# (or text/markdown) path. We allow letters, digits, dash, underscore,
# dot, and forward-slash (for subdirectories), and require a ``.gmi``
# extension. This keeps the editor focused on its job and makes the
# path-safety check simple.
_VALID_RELPATH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")

_feed_signature: FeedSignature | None = None
_feed_refresh_lock: asyncio.Lock | None = None
_feed_error: str | None = None


# ---------------------------------------------------------------- helpers

def _safe_hostname() -> str:
    """Return the configured Gemini hostname or a placeholder."""
    if _VALID_HOSTNAME_RE.match(GEMINI_HOSTNAME):
        return GEMINI_HOSTNAME
    return "your-openhost-zone"


async def _agate_up() -> bool:
    """Return True iff something is listening on 127.0.0.1:1965.

    Short-timeout TCP connect rather than a TLS handshake -- we just
    need "process bound to its port", not "Gemini stream healthy", and
    we don't want to burn CPU per health check. The connect happens in
    a worker thread so the event loop doesn't block on the timeout.
    """

    def _probe() -> bool:
        try:
            with socket.create_connection((AGATE_HOST, AGATE_PORT), timeout=PROBE_TIMEOUT_SECONDS):
                return True
        except OSError:
            return False

    return await asyncio.to_thread(_probe)


def _resolve_content_path(rel: str) -> Path:
    """Resolve ``rel`` (a user-supplied relative path) against
    ``CONTENT_DIR`` and refuse anything that escapes the content dir,
    contains absolute components, or doesn't look like a gemtext file.

    Raises HTTPException with a 4xx status on rejection.
    """
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        raise HTTPException(400, "invalid path")
    if not _VALID_RELPATH_RE.match(rel):
        raise HTTPException(400, "path contains characters that are not allowed")
    if not rel.endswith(".gmi"):
        raise HTTPException(400, "only .gmi files are editable")

    candidate = (CONTENT_DIR / rel).resolve()
    # ``resolve(strict=False)`` follows symlinks. We re-check the
    # parents so a content-dir-relative symlink can't be used to
    # write outside the content dir on a future create-file call.
    try:
        candidate.relative_to(CONTENT_DIR)
    except ValueError:
        raise HTTPException(400, "path escapes the content directory")
    return candidate


def _list_gmi_files() -> list[str]:
    """Return relative paths of ``.gmi`` regular files under
    ``CONTENT_DIR``, sorted. Symlinks (in or out of bounds) are
    skipped: the editor refuses to read or write through symlinks at
    the per-file API, so listing them in the sidebar would just give
    the user clickable entries that 400.

    Errors traversing the directory propagate as HTTPException(500)
    with the underlying ``OSError`` message, mirroring how every
    other file-IO call in this module reports failures.
    """
    if not CONTENT_DIR.is_dir():
        return []
    paths: list[str] = []
    try:
        entries = list(CONTENT_DIR.rglob("*.gmi"))
    except OSError as exc:
        raise HTTPException(500, f"failed to list content dir: {exc}")
    for entry in entries:
        # is_symlink first so a dangling symlink doesn't trip is_file.
        if entry.is_symlink():
            continue
        try:
            if not entry.is_file():
                continue
        except OSError as exc:
            # A subtree we can't stat (e.g. permission flip mid-walk)
            # is not actionable; skip the entry rather than aborting
            # the whole list, but log so the operator can see why a
            # file is missing from the editor.
            logger.warning("skipping %s (cannot stat): %s", entry, exc)
            continue
        try:
            rel = entry.relative_to(CONTENT_DIR)
        except ValueError:
            # Should not happen for a non-symlink under CONTENT_DIR,
            # but be defensive.
            continue
        paths.append(str(rel))
    paths.sort()
    return paths


async def _refresh_feed() -> bool:
    """Regenerate the RSS feed after editor or out-of-band content changes."""
    global _feed_signature
    global _feed_refresh_lock
    global _feed_error
    if _feed_refresh_lock is None:
        _feed_refresh_lock = asyncio.Lock()
    async with _feed_refresh_lock:
        try:
            _feed_signature = await asyncio.to_thread(
                refresh_feed,
                CONTENT_DIR,
                _safe_hostname(),
                _feed_signature,
            )
        except Exception as exc:
            _feed_error = str(exc)
            logger.exception("failed to refresh RSS feed")
            return False
        _feed_error = None
        return True


async def _feed_refresh_loop() -> None:
    while True:
        await asyncio.sleep(FEED_REFRESH_SECONDS)
        await _refresh_feed()


@asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    global _feed_signature
    global _feed_refresh_lock
    _feed_signature = None
    _feed_refresh_lock = asyncio.Lock()
    await _refresh_feed()
    refresh_task = asyncio.create_task(_feed_refresh_loop())
    try:
        yield
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        _feed_refresh_lock = None


# ---------------------------------------------------------------- handlers

_LANDING_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="A text-first Gemini capsule.">
  <title>Gemini capsule | {host}</title>
  <link rel="stylesheet" href="/static/landing.css">
</head>
<body>
  <header class="site-header">
    <div class="nav-inner">
      <a class="brand" href="/" aria-label="Gemini capsule home">
        <span class="brand-pixel" aria-hidden="true"></span>
        <strong>Gemini capsule</strong>
      </a>
      <nav class="nav-links" aria-label="Capsule navigation">
        <a href="#connect">Connect</a>
        <a href="#about">About Gemini</a>
      </nav>
    </div>
  </header>

  <main>
    <section class="hero grid-surface">
      <div class="hero-inner">
        <div class="hero-copy">
          <p class="eyebrow">// small-web publishing</p>
          <h1>A quieter place<br>to publish.</h1>
          <p class="lede">This is a Gemini capsule: a collection of text-first pages served outside the web, at an address of its own.</p>
          <div class="hero-actions">
            <a class="button button-primary" href="gemini://{host}/">Open the capsule</a>
            <a class="button" href="#connect">Get a Gemini client</a>
          </div>
          <p class="hero-note">NO SCRIPTS&nbsp;&nbsp;/&nbsp;&nbsp;NO TRACKING&nbsp;&nbsp;/&nbsp;&nbsp;JUST DOCUMENTS</p>
        </div>

        <div class="endpoint-card" aria-label="Capsule connection details">
          <div class="endpoint-topline">
            <span class="live-dot {status_class}" aria-hidden="true"></span>
            <span>{status_text}</span>
          </div>
          <code class="endpoint-url">gemini://{host}/</code>
          <dl class="endpoint-facts">
            <div><dt>Protocol</dt><dd>Gemini</dd></div>
            <div><dt>Port</dt><dd>1965 / TLS</dd></div>
            <div><dt>Format</dt><dd>text/gemini</dd></div>
          </dl>
        </div>
      </div>
    </section>

    <section class="section connect" id="connect">
      <p class="eyebrow">// connect</p>
      <div class="section-heading">
        <div>
          <h2>Open this capsule in a Gemini client.</h2>
          <p>Gemini uses its own URL scheme, so a normal web browser cannot display the capsule itself.</p>
        </div>
        <code class="address">gemini://{host}/</code>
      </div>

      <div class="client-grid">
        <a class="client-card" href="https://lagrange.skyjake.fi/" target="_blank" rel="noopener noreferrer">
          <span class="card-number">01</span>
          <h3>Lagrange</h3>
          <p>A graphical client for desktop and mobile.</p>
          <span class="card-link">Visit Lagrange &rarr;</span>
        </a>
        <a class="client-card" href="https://github.com/makew0rld/amfora" target="_blank" rel="noopener noreferrer">
          <span class="card-number">02</span>
          <h3>Amfora</h3>
          <p>A keyboard-friendly Gemini client for the terminal.</p>
          <span class="card-link">View on GitHub &rarr;</span>
        </a>
        <a class="client-card client-card-accent" href="https://geminiprotocol.net/software/" target="_blank" rel="noopener noreferrer">
          <span class="card-number">03</span>
          <h3>More clients</h3>
          <p>Browse clients for every major platform.</p>
          <span class="card-link">See the directory &rarr;</span>
        </a>
      </div>

      <div class="feed-callout">
        <div class="feed-copy">
          <span class="card-number">RSS</span>
          <h3>Follow this gemlog in Newsboat.</h3>
          <p>Dated pages in the posts directory are published automatically.</p>
        </div>
        <code>&quot;exec:gemget -o - gemini://{host}/feed.rss&quot;</code>
      </div>

    </section>

    <section class="about-band grid-surface" id="about">
      <div class="about-inner">
        <div class="about-copy">
          <p class="eyebrow">// why Gemini</p>
          <h2>The web, pared back to reading and writing.</h2>
          <p>Gemini is a small internet protocol for serving documents. Its intentionally narrow scope keeps pages fast, legible, and quiet.</p>
          <a class="text-link" href="https://geminiprotocol.net/" target="_blank" rel="noopener noreferrer">Read about the protocol &rarr;</a>
        </div>
        <div class="fact-list">
          <article>
            <span>01</span>
            <div><h3>Text first</h3><p>Gemtext has only six line shapes and no embedded scripts.</p></div>
          </article>
          <article>
            <span>02</span>
            <div><h3>Private by default</h3><p>No cookies, trackers, pop-ups, or behavioral advertising.</p></div>
          </article>
          <article>
            <span>03</span>
            <div><h3>Built to last</h3><p>Small pages and a simple protocol make archives easy to keep.</p></div>
          </article>
        </div>
      </div>
    </section>
  </main>

  <footer class="footer">
    <div class="footer-inner">
      <div>
        <strong>Gemini capsule</strong>
        <p>Small pages. Quiet protocol.</p>
      </div>
      <a href="gemini://{host}/">gemini://{host}/</a>
    </div>
  </footer>
</body>
</html>
"""

def _is_owner(request: Request) -> bool:
    """Return True when OpenHost forwarded the X-OpenHost-Is-Owner
    header on the proxied request. Owners are sent straight to the editor;
    the actual auth boundary is enforced separately by ``_owner_only``."""
    return request.headers.get("X-OpenHost-Is-Owner", "").lower() == "true"


async def landing(request: Request) -> Response:
    if _is_owner(request) and request.query_params.get("public") != "1":
        return Response(status_code=302, headers={"Location": "/edit", "Cache-Control": "no-store"})

    agate_up = await _agate_up()
    body = _LANDING_TEMPLATE.format(
        host=html.escape(_safe_hostname(), quote=True),
        status_class="is-online" if agate_up else "is-offline",
        status_text="Capsule online" if agate_up else "Capsule unavailable",
    )
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


async def healthz(request: Request) -> Response:
    if not await _agate_up():
        return PlainTextResponse("agate-not-listening\n", status_code=503)
    if _feed_error is not None:
        return PlainTextResponse("rss-feed-error\n", status_code=503)
    return PlainTextResponse("ok\n")


def _owner_only(request: Request) -> None:
    """Reject the request unless OpenHost forwarded an authenticated
    owner. OpenHost adds ``X-OpenHost-Is-Owner: true`` to proxied
    requests when the JWT's ``sub`` claim is ``owner`` (see
    ``compute_space.web.routes.proxy._identity_headers``). Anonymous
    requests reach us without the header, so we use its presence as
    a clean owner-vs-public signal.

    We must do this auth check ourselves rather than relying on
    OpenHost's ``public_paths`` filter because the matcher treats
    ``/`` as a prefix that matches every URL. We therefore declare
    the entire app public to OpenHost (so the unauth landing page
    works) and gate the editor and file API in this sidecar.

    For requests that don't pass through OpenHost (e.g. someone
    addressing the container's localhost port directly), the header
    is absent, so this still defaults to deny.
    """
    if request.headers.get("X-OpenHost-Is-Owner", "").lower() != "true":
        # Mirror OpenHost's behaviour for unauth requests on private
        # paths: redirect HTML clients to the OpenHost sign-in page.
        # For API clients (XHR / fetch) a 401 with a JSON body is
        # more useful than a 302.
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            # OpenHost-derived host; build /login on the bare
            # zone domain via the X-Forwarded-Host header it sets.
            zone = request.headers.get("X-Forwarded-Host", "")
            if zone:
                # Strip the app subdomain so we land on the bare
                # zone's /login page (matches OpenHost's own auth
                # redirect target).
                bare = zone.split(".", 1)[1] if "." in zone else zone
                login_url = f"https://{bare}/login"
                raise HTTPException(302, headers={"location": login_url})
        raise HTTPException(401, "this route requires an OpenHost session")


async def edit_page(request: Request) -> HTMLResponse:
    """Serve the editor shell. The actual content + file list is
    populated by ``editor.js`` calling ``/api/files``."""
    _owner_only(request)
    try:
        template = (TEMPLATES_DIR / "editor.html").read_text(encoding="utf-8")
    except OSError as exc:
        # Should be impossible at runtime (the file is COPYed into the
        # image), but a broken build or hand-mounted volume could
        # remove it. Surface a 500 with context instead of letting an
        # unhandled exception become an opaque ASGI traceback.
        raise HTTPException(500, f"editor template missing: {exc}")
    return HTMLResponse(template, headers={"Cache-Control": "no-store"})


async def list_files(request: Request) -> JSONResponse:
    _owner_only(request)
    return JSONResponse({"files": _list_gmi_files()})


async def get_file(request: Request) -> JSONResponse:
    _owner_only(request)
    rel = request.path_params["rel"]
    path = _resolve_content_path(rel)
    # is_symlink first: Path.is_file follows symlinks, so a symlink to
    # a regular file would pass is_file and only get rejected by the
    # symlink check; a symlink to a directory would raise the "not a
    # regular file" error which is misleading. Refuse all symlinks
    # uniformly so every symlinked path produces the same clear
    # error, regardless of what it points at.
    if path.is_symlink():
        raise HTTPException(400, "symlinks are not editable")
    if not path.exists():
        raise HTTPException(404, f"no such file: {rel}")
    if not path.is_file():
        raise HTTPException(400, "path is not a regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(500, f"failed to read: {exc}")
    return JSONResponse({"path": rel, "content": text})


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_FILE_BYTES + 1024:
        # 1 KiB headroom for the JSON envelope.
        raise HTTPException(413, "request body too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"invalid JSON body: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON body must be an object")
    return data


def _validate_content(value: Any, *, missing_message: str = "missing 'content' field") -> str:
    """Validate that ``value`` is a UTF-8-encodable string under the
    file size cap. Raises HTTPException with a 4xx detail on rejection.

    Distinguishes ``None`` (key absent) from a non-string value so the
    error message matches the user's actual mistake.
    """
    if value is None:
        raise HTTPException(400, missing_message)
    if not isinstance(value, str):
        raise HTTPException(400, "'content' must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        # Python str can hold lone surrogates that JSON allows but
        # UTF-8 cannot encode; reject with a clear 400 instead of
        # letting the unhandled exception become a 500.
        raise HTTPException(400, f"content is not valid UTF-8: {exc}")
    if len(encoded) > MAX_FILE_BYTES:
        raise HTTPException(413, f"content exceeds {MAX_FILE_BYTES} bytes")
    return value


async def put_file(request: Request) -> JSONResponse:
    """Overwrite an existing file. Will not create a new file -- use
    POST for that, so accidental misspellings of an existing path
    don't silently create a stray file."""
    _owner_only(request)
    rel = request.path_params["rel"]
    path = _resolve_content_path(rel)
    if not path.exists():
        raise HTTPException(404, f"no such file: {rel} (use POST to create)")
    if path.is_symlink():
        raise HTTPException(400, "symlinks are not editable")
    if not path.is_file():
        raise HTTPException(400, "path is not a regular file")

    data = await _read_json_body(request)
    content = _validate_content(
        data.get("content"),
        missing_message="missing 'content' field (use POST to create new files with default content)",
    )

    # Atomic write: write to a sibling tempfile in the same dir then
    # replace, so a crash mid-write doesn't truncate the existing
    # file. ``Path.replace`` is atomic on the same filesystem.
    #
    # Use a per-call unique temp filename via tempfile.mkstemp so two
    # concurrent saves of the same path don't clobber each other's
    # ``.partial`` file. mkstemp creates the file atomically so there
    # is no TOCTOU window either.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path.replace(path)
    except OSError as exc:
        # Best-effort cleanup; if even the unlink fails we log so the
        # operator at least sees the orphan temp file rather than
        # discovering it later by accident.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            logger.warning(
                "failed to write %s and could not remove temp %s: %s",
                path, tmp_path, cleanup_exc,
            )
        raise HTTPException(500, f"failed to write: {exc}")
    feed_ok = await _refresh_feed()
    return JSONResponse(
        {"path": rel, "bytes": len(content.encode("utf-8")), "feed_status": "ok" if feed_ok else "error"}
    )


async def post_file(request: Request) -> JSONResponse:
    """Create a new file. Will not overwrite an existing file -- use
    PUT for that. Creates intermediate directories as needed (still
    confined to CONTENT_DIR by ``_resolve_content_path``)."""
    _owner_only(request)
    rel = request.path_params["rel"]
    path = _resolve_content_path(rel)
    if path.exists():
        raise HTTPException(409, f"already exists: {rel}")

    data = await _read_json_body(request)
    content = _validate_content(data.get("content", ""))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``open(..., 'x')`` is exclusive: fails if the file appeared
        # between our exists() check and now (TOCTOU).
        with path.open("x", encoding="utf-8") as f:
            f.write(content)
    except FileExistsError:
        raise HTTPException(409, f"already exists: {rel}")
    except OSError as exc:
        raise HTTPException(500, f"failed to create: {exc}")
    feed_ok = await _refresh_feed()
    return JSONResponse(
        {"path": rel, "bytes": len(content.encode("utf-8")), "feed_status": "ok" if feed_ok else "error"},
        status_code=201,
    )


async def delete_file(request: Request) -> Response:
    _owner_only(request)
    rel = request.path_params["rel"]
    path = _resolve_content_path(rel)
    if not path.exists():
        raise HTTPException(404, f"no such file: {rel}")
    if path.is_symlink() or not path.is_file():
        raise HTTPException(400, "only regular files can be deleted")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(500, f"failed to delete: {exc}")
    feed_ok = await _refresh_feed()
    return Response(status_code=204, headers={"X-RSS-Feed-Status": "ok" if feed_ok else "error"})


# ----------------------------------------------------------------- error handler

async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Return JSON for /api/* errors, plain text for other errors,
    and a redirect (no body) for 3xx with a ``location`` header."""
    # 3xx redirects only need the Location header; do not write the
    # detail string into the body.
    if 300 <= exc.status_code < 400:
        location = (exc.headers or {}).get("location", "")
        return Response(status_code=exc.status_code, headers={"location": location})
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return PlainTextResponse(str(exc.detail) + "\n", status_code=exc.status_code)


# ----------------------------------------------------------------- app

# The path converter handles the multi-segment relative paths the
# editor uses (e.g. ``api/files/notes/2026.gmi``). Starlette's default
# ``str`` converter rejects slashes; ``path`` accepts them.
routes = [
    Route("/", landing),
    Route("/healthz", healthz),
    Route("/edit", edit_page),
    Route("/api/files", list_files),
    Route("/api/files/{rel:path}", get_file, methods=["GET"]),
    Route("/api/files/{rel:path}", put_file, methods=["PUT"]),
    Route("/api/files/{rel:path}", post_file, methods=["POST"]),
    Route("/api/files/{rel:path}", delete_file, methods=["DELETE"]),
]

app: Starlette = Starlette(
    debug=False,
    routes=routes,
    exception_handlers={HTTPException: http_exception_handler},
    lifespan=lifespan,
)

# Mount static assets under /static (loaded by editor.html).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
