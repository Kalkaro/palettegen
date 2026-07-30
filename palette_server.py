#!/usr/bin/env python3
"""Hardened Konachan + Stylix palette showcase server."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import warnings
from collections import defaultdict, deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, UnidentifiedImageError


ROOT = Path(__file__).resolve().parent
HTML = ROOT / "palette-showcase.html"
HISTORY = ROOT / "palette-history"
API_URL = "https://konachan.net/post.json?tags=order:random+rating:safe&limit=1"
USER_AGENT = "Mozilla/5.0 (Linux; Stylix palette showcase)"
NIX_PORTABLE = Path(
    os.environ.get("PALETTE_NIX_PORTABLE", "/tmp/nix-portable-x86_64")
).resolve()
GENERATOR_FLAKE = os.environ.get(
    "PALETTE_GENERATOR_FLAKE",
    "github:nix-community/stylix/66714e5ce44269ecc58c20d9196da8dbe1b27a31"
    "#palette-generator",
)
HOST = os.environ.get("PALETTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PALETTE_PORT", "8765"))
PUBLIC_MODE = os.environ.get("PALETTE_PUBLIC", "").lower() in {"1", "true", "yes"}
AUTH_USERNAME = os.environ.get("PALETTE_USERNAME", "palette")
AUTH_PASSWORD = os.environ.get("PALETTE_PASSWORD", "")
MAX_IMAGE_BYTES = int(os.environ.get("PALETTE_MAX_IMAGE_MB", "25")) * 1024 * 1024
MAX_IMAGE_PIXELS = int(os.environ.get("PALETTE_MAX_IMAGE_PIXELS", "50000000"))
MAX_HISTORY_ITEMS = int(os.environ.get("PALETTE_MAX_HISTORY", "100"))
MAX_HISTORY_BYTES = int(os.environ.get("PALETTE_MAX_HISTORY_MB", "1024")) * 1024 * 1024
RATE_LIMIT_COUNT = int(os.environ.get("PALETTE_RATE_LIMIT", "0"))
RATE_LIMIT_WINDOW = 10 * 60
ALLOWED_IMAGE_HOSTS = {"konachan.net"}
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
generation_lock = threading.Lock()
active_job_lock = threading.Lock()
active_job: dict[str, object] | None = None
RECORD_ID = re.compile(r"^[0-9TZ-]+$")
HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
rate_lock = threading.Lock()
request_times: defaultdict[str, deque[float]] = defaultdict(deque)
auth_failures: defaultdict[str, deque[float]] = defaultdict(deque)

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request:
        validate_remote_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


URL_OPENER = urllib.request.build_opener(SafeRedirectHandler)


def validate_remote_url(url: object) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("Invalid wallpaper URL")
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not any(
            hostname == allowed or hostname.endswith(f".{allowed}")
            for allowed in ALLOWED_IMAGE_HOSTS
        )
    ):
        raise ValueError("Wallpaper URL is not on an allowed HTTPS host")
    return url


def request(url: str, timeout: int) -> urllib.request.addinfourl:
    validate_remote_url(url)
    return URL_OPENER.open(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
        timeout=timeout,
    )


def read_limited_json(response: urllib.request.addinfourl, limit: int = 1024 * 1024) -> object:
    declared = response.headers.get("Content-Length")
    if declared and int(declared) > limit:
        raise ValueError("Remote API response is too large")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Remote API response is too large")
    return json.loads(data)


def download_limited(url: str, destination: Path) -> str:
    with request(url, 60) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_IMAGE_BYTES:
            raise ValueError("Wallpaper exceeds the download limit")
        size = 0
        with destination.open("wb") as output:
            os.chmod(destination, 0o600)
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError("Wallpaper exceeds the download limit")
                output.write(chunk)
    if size == 0:
        raise ValueError("Wallpaper download was empty")
    return validate_image(destination)


def validate_image(path: Path) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                frames = getattr(image, "n_frames", 1)
                if image_format not in IMAGE_TYPES:
                    raise ValueError("Wallpaper format is not allowed")
                if width < 320 or height < 200 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("Wallpaper dimensions are outside the allowed range")
                if frames != 1:
                    raise ValueError("Animated wallpapers are not allowed")
                image.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError("Downloaded file is not a safe supported image") from error
    return IMAGE_TYPES[image_format]


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_record(path: Path) -> dict[str, object]:
    if path.stat().st_size > 128 * 1024:
        raise ValueError("Metadata file is too large")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Invalid metadata")
    return record


def nix_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256-" + base64.b64encode(digest.digest()).decode()


def public_record(record: dict[str, object]) -> dict[str, object]:
    result = record.copy()
    result.pop("content_type", None)
    return result


def prune_history() -> None:
    if not HISTORY.is_dir():
        return
    candidates: list[tuple[Path, int]] = []
    for directory in sorted(HISTORY.iterdir(), key=lambda item: item.name, reverse=True):
        if not directory.is_dir() or not RECORD_ID.fullmatch(directory.name):
            continue
        size = sum(
            item.stat().st_size
            for item in directory.iterdir()
            if item.is_file() and not item.is_symlink()
        )
        candidates.append((directory, size))

    retained_count = 0
    retained_size = 0
    for directory, size in candidates:
        if (
            retained_count < MAX_HISTORY_ITEMS
            and retained_size + size <= MAX_HISTORY_BYTES
        ):
            retained_count += 1
            retained_size += size
            continue
        shutil.rmtree(directory)


def validate_runtime() -> None:
    if not NIX_PORTABLE.is_file() or NIX_PORTABLE.is_symlink():
        raise RuntimeError("The configured Stylix runtime is missing or unsafe")
    runtime_stat = NIX_PORTABLE.stat()
    if runtime_stat.st_uid != os.geteuid() or runtime_stat.st_mode & 0o022:
        raise RuntimeError(
            "The Stylix runtime must be owned by the service user and not writable "
            "by group or others"
        )


def start_generation(polarity: str = "dark") -> dict[str, object]:
    global active_job

    if polarity not in {"dark", "light"}:
        raise ValueError("Invalid palette polarity")
    validate_runtime()

    with request(API_URL, 30) as response:
        posts = read_limited_json(response)
    if not isinstance(posts, list) or not posts or not isinstance(posts[0], dict):
        raise ValueError("Wallpaper provider returned an invalid response")

    post = posts[0]
    post_id = post.get("id")
    if not isinstance(post_id, int) or post_id <= 0:
        raise ValueError("Wallpaper provider returned an invalid post ID")
    image_url = validate_remote_url(post.get("file_url"))

    descriptor, temporary_name = tempfile.mkstemp(dir=ROOT, prefix=".wallpaper-")
    os.close(descriptor)
    temporary_wallpaper = Path(temporary_name)
    try:
        content_type = download_limited(image_url, temporary_wallpaper)
    except Exception:
        temporary_wallpaper.unlink(missing_ok=True)
        raise

    created_at = datetime.now(timezone.utc)
    record_id = f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{post_id}"
    record_dir = HISTORY / record_id
    record_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.replace(temporary_wallpaper, record_dir / "wallpaper")
    os.chmod(record_dir / "wallpaper", 0o600)

    metadata = {
        "id": record_id,
        "image": f"/history/{record_id}/wallpaper",
        "tags": str(post.get("tags", ""))[:4096],
        "post_url": f"https://konachan.net/post/show/{post_id}",
        "image_url": image_url,
        "sha256": nix_sha256(record_dir / "wallpaper"),
        "created_at": created_at.isoformat(),
        "content_type": content_type,
        "polarity": polarity,
        "status": "generating",
    }
    atomic_write_json(record_dir / "metadata.json", metadata)

    job: dict[str, object] = {
        "id": record_id,
        "polarity": polarity,
        "cancel": threading.Event(),
        "process": None,
    }
    with active_job_lock:
        active_job = job

    threading.Thread(
        target=finish_generation,
        args=(job,),
        name=f"stylix-{record_id}",
        daemon=True,
    ).start()
    return public_record(metadata)


def finish_generation(job: dict[str, object]) -> None:
    global active_job

    record_id = str(job["id"])
    cancel = job["cancel"]
    assert isinstance(cancel, threading.Event)
    record_dir = HISTORY / record_id
    metadata_file = record_dir / "metadata.json"
    temporary_palette = record_dir / ".palette.json"

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PALETTE_")
    }
    environment.update({"NP_RUNTIME": "proot", "NP_GIT": "/usr/bin/git"})
    command = [
        str(NIX_PORTABLE),
        "nix",
        "run",
        GENERATOR_FLAKE,
        "--",
        str(job["polarity"]),
        str(record_dir / "wallpaper"),
        str(temporary_palette),
    ]

    try:
        if cancel.is_set():
            raise InterruptedError("Palette generation skipped")

        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with active_job_lock:
            job["process"] = process
            should_cancel = cancel.is_set()
        if should_cancel:
            os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=240)
        if cancel.is_set():
            raise InterruptedError("Palette generation skipped")
        if process.returncode:
            raise RuntimeError(stderr.strip() or stdout.strip() or "Stylix generation failed")

        if temporary_palette.stat().st_size > 64 * 1024:
            raise RuntimeError("Stylix returned an oversized palette")
        palette = json.loads(temporary_palette.read_text(encoding="utf-8"))
        if (
            not isinstance(palette, dict)
            or set(palette) != {f"base{i:02X}" for i in range(16)}
            or not all(isinstance(value, str) and HEX_COLOR.fullmatch(value) for value in palette.values())
        ):
            raise RuntimeError("Stylix returned an incomplete palette")
        os.replace(temporary_palette, record_dir / "palette.json")
        os.chmod(record_dir / "palette.json", 0o600)
        metadata = load_record(metadata_file)
        metadata.update({"palette": palette, "status": "ready"})
        atomic_write_json(metadata_file, metadata)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        error = RuntimeError("Stylix generation timed out")
        temporary_palette.unlink(missing_ok=True)
        metadata = load_record(metadata_file)
        metadata.update({"status": "error", "error": "Palette generation timed out"})
        atomic_write_json(metadata_file, metadata)
    except Exception as error:
        temporary_palette.unlink(missing_ok=True)
        print(f"[palette] generation {record_id} failed: {type(error).__name__}: {error}")
        try:
            metadata = load_record(metadata_file)
            status = "skipped" if cancel.is_set() else "error"
            message = "Palette generation skipped" if status == "skipped" else "Palette generation failed"
            metadata.update({"status": status, "error": message})
            atomic_write_json(metadata_file, metadata)
        except (OSError, ValueError):
            pass
    finally:
        with active_job_lock:
            if active_job is job:
                active_job = None
        try:
            prune_history()
        except OSError as error:
            print(f"[palette] history cleanup failed: {error}")
        generation_lock.release()


def cancel_active_generation() -> bool:
    with active_job_lock:
        job = active_job
        if job is None:
            return False
        cancel = job["cancel"]
        assert isinstance(cancel, threading.Event)
        cancel.set()
        process = job.get("process")

    if isinstance(process, subprocess.Popen) and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return True


def generation_status(record_id: str) -> dict[str, object] | None:
    if not RECORD_ID.fullmatch(record_id):
        return None
    metadata_file = HISTORY / record_id / "metadata.json"
    if not metadata_file.is_file() or metadata_file.is_symlink():
        return None
    record = load_record(metadata_file)
    if "status" not in record:
        record["status"] = "ready" if record.get("palette") else "error"
    return public_record(record)


def history() -> list[dict[str, object]]:
    if not HISTORY.is_dir():
        return []
    records = []
    for metadata_file in sorted(HISTORY.glob("*/metadata.json"), reverse=True):
        try:
            if (
                not RECORD_ID.fullmatch(metadata_file.parent.name)
                or metadata_file.is_symlink()
            ):
                continue
            record = load_record(metadata_file)
            if record.get("status", "ready") != "ready" or not record.get("palette"):
                continue
            wallpaper = metadata_file.parent / "wallpaper"
            changed = False
            if wallpaper.is_file() and not wallpaper.is_symlink() and not record.get("sha256"):
                record["sha256"] = nix_sha256(wallpaper)
                changed = True
            if changed:
                atomic_write_json(metadata_file, record)
            records.append(public_record(record))
        except (OSError, ValueError):
            continue
    return records[:MAX_HISTORY_ITEMS]


def inline_script_hash() -> str:
    html = HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, flags=re.DOTALL)
    if not match:
        raise RuntimeError("The page is missing its inline script")
    digest = hashlib.sha256(match.group(1).encode()).digest()
    return base64.b64encode(digest).decode()


SCRIPT_HASH = inline_script_hash()
CSP = (
    "default-src 'self'; "
    f"script-src 'self' 'sha256-{SCRIPT_HASH}'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def within_limit(
    buckets: defaultdict[str, deque[float]],
    key: str,
    limit: int,
) -> tuple[bool, int]:
    now = time.monotonic()
    with rate_lock:
        timestamps = buckets[key]
        while timestamps and timestamps[0] <= now - RATE_LIMIT_WINDOW:
            timestamps.popleft()
        if len(timestamps) >= limit:
            retry_after = max(1, int(RATE_LIMIT_WINDOW - (now - timestamps[0])))
            return False, retry_after
        timestamps.append(now)
        if len(buckets) > 2048:
            for stale_key in list(buckets):
                if not buckets[stale_key] or buckets[stale_key][-1] <= now - RATE_LIMIT_WINDOW:
                    del buckets[stale_key]
        return True, 0


def request_allowed(client: str) -> tuple[bool, int]:
    if RATE_LIMIT_COUNT <= 0:
        return True, 0
    allowed, retry_after = within_limit(request_times, client, RATE_LIMIT_COUNT)
    if not allowed:
        return allowed, retry_after
    return within_limit(request_times, "__all_clients__", RATE_LIMIT_COUNT * 3)


class Handler(BaseHTTPRequestHandler):
    server_version = "palette"
    sys_version = ""

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def send_bytes(
        self,
        data: bytes,
        content_type: str,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        self.send_bytes(
            json.dumps(payload).encode(),
            "application/json; charset=utf-8",
            status,
        )

    def send_file(self, path: Path, content_type: str) -> None:
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        try:
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=64 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self) -> bool:
        if not AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            self.require_authentication()
            return False
        try:
            raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = raw.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            self.require_authentication()
            return False
        allowed = hmac.compare_digest(username, AUTH_USERNAME) & hmac.compare_digest(
            password, AUTH_PASSWORD
        )
        if not allowed:
            attempts_allowed, retry_after = within_limit(
                auth_failures, self.client_key(), 20
            )
            if not attempts_allowed:
                self.send_bytes(
                    b"too many authentication attempts\n",
                    "text/plain; charset=utf-8",
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"Retry-After": str(retry_after)},
                )
                return False
            time.sleep(0.15)
            self.require_authentication()
        return bool(allowed)

    def require_authentication(self) -> None:
        self.send_bytes(
            b"authentication required\n",
            "text/plain; charset=utf-8",
            HTTPStatus.UNAUTHORIZED,
            {"WWW-Authenticate": 'Basic realm="palette generator", charset="UTF-8"'},
        )

    def client_key(self) -> str:
        direct = self.client_address[0]
        forwarded = self.headers.get("X-Real-IP", "")
        if PUBLIC_MODE and direct in {"127.0.0.1", "::1"} and forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return direct

    def same_origin(self) -> bool:
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        host = self.headers.get("Host", "")
        return (
            parsed.scheme in {"http", "https"}
            and parsed.netloc.lower() == host.lower()
            and len(host) <= 255
        )

    def do_GET(self) -> None:
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if path in {"/", "/palette-showcase.html"}:
            self.send_bytes(HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/history":
            self.send_json({"history": history()})
        elif path.startswith("/api/generation/"):
            record_id = path.rsplit("/", 1)[-1]
            record = generation_status(record_id)
            if record is None:
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self.send_json(record)
        elif path.startswith("/history/") and path.endswith("/wallpaper"):
            parts = path.strip("/").split("/")
            record_id = parts[1] if len(parts) == 3 else ""
            wallpaper = HISTORY / record_id / "wallpaper"
            metadata_file = HISTORY / record_id / "metadata.json"
            if (
                not RECORD_ID.fullmatch(record_id)
                or not wallpaper.is_file()
                or wallpaper.is_symlink()
            ):
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            content_type = "image/jpeg"
            if metadata_file.is_file() and not metadata_file.is_symlink():
                stored_type = load_record(metadata_file).get("content_type")
                if stored_type in IMAGE_TYPES.values():
                    content_type = str(stored_type)
            self.send_file(wallpaper, content_type)
        else:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/api/generate":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.same_origin():
            self.send_json({"error": "Cross-origin request denied"}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self.send_json({"error": "Request body is not allowed"}, HTTPStatus.BAD_REQUEST)
            return
        allowed, retry_after = request_allowed(self.client_key())
        if not allowed:
            self.send_bytes(
                json.dumps({"error": "Too many generation requests"}).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.TOO_MANY_REQUESTS,
                {"Retry-After": str(retry_after)},
            )
            return
        parameters = parse_qs(parsed.query)
        polarity_values = parameters.get("polarity", ["dark"])
        if len(polarity_values) != 1 or polarity_values[0] not in {"dark", "light"}:
            self.send_json({"error": "Invalid palette polarity"}, HTTPStatus.BAD_REQUEST)
            return
        polarity = polarity_values[0]
        should_skip = parameters.get("skip") == ["1"]
        if should_skip:
            cancel_active_generation()
            acquired = generation_lock.acquire(timeout=15)
        else:
            acquired = generation_lock.acquire(blocking=False)
        if not acquired:
            self.send_json({"error": "A palette is already being generated"}, 409)
            return
        try:
            self.send_json(start_generation(polarity), 202)
        except Exception as error:
            generation_lock.release()
            print(f"[palette] request failed: {type(error).__name__}: {error}")
            self.send_json({"error": "Could not generate a wallpaper"}, 502)

    def log_message(self, message: str, *args: object) -> None:
        rendered = (message % args).replace("\r", "").replace("\n", "")
        print(f"[palette] {self.client_address[0]} — {rendered[:1000]}")


class HardenedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    connection_slots = threading.BoundedSemaphore(32)

    def process_request(self, request: object, client_address: object) -> None:
        if not self.connection_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def get_request(self) -> tuple[object, object]:
        request_socket, address = super().get_request()
        request_socket.settimeout(15)
        return request_socket, address


if __name__ == "__main__":
    if PUBLIC_MODE and len(AUTH_PASSWORD) < 16:
        raise SystemExit(
            "PALETTE_PUBLIC=1 requires a PALETTE_PASSWORD of at least 16 characters"
        )
    if HOST not in {"127.0.0.1", "::1", "localhost"} and len(AUTH_PASSWORD) < 16:
        raise SystemExit(
            "A non-loopback PALETTE_HOST requires a PALETTE_PASSWORD of at least 16 characters"
        )
    HISTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    validate_runtime()
    prune_history()
    server = HardenedHTTPServer((HOST, PORT), Handler)
    print(f"Wallpaper palette: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
