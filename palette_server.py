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
import secrets
import signal
import shutil
import socket
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
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunsplit

from PIL import Image, UnidentifiedImageError
from urllib3 import HTTPSConnectionPool, Timeout


ROOT = Path(__file__).resolve().parent
HTML = ROOT / "palette-showcase.html"
DATA_DIR_CONFIG = os.environ.get("PALETTE_DATA_DIR", "").strip()
DATA_HOME_CONFIG = os.environ.get("XDG_DATA_HOME", "").strip()
DEFAULT_DATA_HOME = (
    Path(DATA_HOME_CONFIG).expanduser().resolve()
    if DATA_HOME_CONFIG
    else Path.home() / ".local" / "share"
)
DATA_DIR = (
    Path(DATA_DIR_CONFIG).expanduser().resolve()
    if DATA_DIR_CONFIG
    else DEFAULT_DATA_HOME / "palette-generator"
)
HISTORY = DATA_DIR
MIN_WALLPAPER_WIDTH = 2560
MIN_WALLPAPER_HEIGHT = 1440
API_URL = "https://konachan.net/post.json?" + urlencode(
    {
        "tags": (
            "order:random rating:safe "
            f"width:>={MIN_WALLPAPER_WIDTH} height:>={MIN_WALLPAPER_HEIGHT}"
        ),
        "limit": 1,
    }
)
USER_AGENT = "Mozilla/5.0 (Linux; Stylix palette showcase)"
NIX_PORTABLE_CONFIG = os.environ.get("PALETTE_NIX_PORTABLE", "").strip()
NIX_PORTABLE = (
    Path(NIX_PORTABLE_CONFIG).expanduser().resolve() if NIX_PORTABLE_CONFIG else None
)
NIX_COMMAND = os.environ.get("PALETTE_NIX", "").strip()
GENERATOR_FLAKE = os.environ.get(
    "PALETTE_GENERATOR_FLAKE",
    "github:nix-community/stylix/66714e5ce44269ecc58c20d9196da8dbe1b27a31"
    "#palette-generator",
)
HOST = os.environ.get("PALETTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PALETTE_PORT", "8766"))
PUBLIC_MODE = os.environ.get("PALETTE_PUBLIC", "").lower() in {"1", "true", "yes"}
AUTH_USERNAME = os.environ.get("PALETTE_USERNAME", "palette")
AUTH_PASSWORD = os.environ.get("PALETTE_PASSWORD", "")
MAX_IMAGE_BYTES = int(os.environ.get("PALETTE_MAX_IMAGE_MB", "25")) * 1024 * 1024
MAX_IMAGE_PIXELS = int(os.environ.get("PALETTE_MAX_IMAGE_PIXELS", "50000000"))
MAX_HISTORY_ITEMS = int(os.environ.get("PALETTE_MAX_HISTORY", "0"))
MAX_HISTORY_BYTES = int(os.environ.get("PALETTE_MAX_HISTORY_MB", "0")) * 1024 * 1024
RATE_LIMIT_COUNT = int(os.environ.get("PALETTE_RATE_LIMIT", "0"))
RATE_LIMIT_WINDOW = 10 * 60
ALLOWED_IMAGE_HOSTS = {"konachan.net"}
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_CONCURRENT_GENERATIONS = 4
generation_slots = threading.BoundedSemaphore(MAX_CONCURRENT_GENERATIONS)
active_jobs_lock = threading.Lock()
active_jobs: dict[str, dict[str, object]] = {}
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


def read_limited_json(
    response: urllib.request.addinfourl, limit: int = 1024 * 1024
) -> object:
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


def resolve_public_https_url(url: object) -> tuple[str, str, list[str]]:
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise ValueError("Invalid image URL")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Image URL must use public HTTPS")
    hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
    try:
        addresses = {
            result[4][0].split("%", 1)[0]
            for result in socket.getaddrinfo(
                hostname, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        }
    except socket.gaierror as error:
        raise ValueError("Image hostname could not be resolved") from error
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise ValueError("Private or reserved image addresses are not allowed")
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return hostname, target, sorted(addresses)


def download_public_image(url: str, destination: Path) -> tuple[str, str]:
    current_url = url
    for redirect_count in range(4):
        hostname, target, addresses = resolve_public_https_url(current_url)
        last_error: Exception | None = None
        response = None
        pool = None
        for address in addresses:
            try:
                pool = HTTPSConnectionPool(
                    address,
                    port=443,
                    timeout=Timeout(connect=10, read=60),
                    retries=False,
                    cert_reqs="CERT_REQUIRED",
                    assert_hostname=hostname,
                    server_hostname=hostname,
                )
                response = pool.request(
                    "GET",
                    target,
                    headers={"Host": hostname, "User-Agent": USER_AGENT},
                    preload_content=False,
                    redirect=False,
                )
                break
            except Exception as error:
                last_error = error
                if pool is not None:
                    pool.close()
        if response is None or pool is None:
            raise ValueError("Image host could not be reached") from last_error

        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location or redirect_count == 3:
                    raise ValueError("Image URL redirected too many times")
                current_url = urljoin(current_url, location)
                continue
            if response.status != HTTPStatus.OK:
                raise ValueError(f"Image server returned HTTP {response.status}")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the download limit")
            size = 0
            with destination.open("wb") as output:
                os.chmod(destination, 0o600)
                for chunk in response.stream(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ValueError("Image exceeds the download limit")
                    output.write(chunk)
            if size == 0:
                raise ValueError("Image download was empty")
            return validate_image(destination), current_url
        finally:
            response.release_conn()
            pool.close()
    raise ValueError("Image URL redirected too many times")


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
                    raise ValueError(
                        "Wallpaper dimensions are outside the allowed range"
                    )
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
    if not HISTORY.is_dir() or (MAX_HISTORY_ITEMS <= 0 and MAX_HISTORY_BYTES <= 0):
        return
    with active_jobs_lock:
        active_record_ids = set(active_jobs)
    candidates: list[tuple[Path, int]] = []
    for directory in sorted(
        HISTORY.iterdir(), key=lambda item: item.name, reverse=True
    ):
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
        if directory.name in active_record_ids:
            continue
        if (MAX_HISTORY_ITEMS <= 0 or retained_count < MAX_HISTORY_ITEMS) and (
            MAX_HISTORY_BYTES <= 0 or retained_size + size <= MAX_HISTORY_BYTES
        ):
            retained_count += 1
            retained_size += size
            continue
        shutil.rmtree(directory)


def nix_runner() -> tuple[list[str], dict[str, str]]:
    if NIX_PORTABLE is not None:
        if not NIX_PORTABLE.is_file() or NIX_PORTABLE.is_symlink():
            raise RuntimeError("The configured Stylix runtime is missing or unsafe")
        runtime_stat = NIX_PORTABLE.stat()
        if runtime_stat.st_uid != os.geteuid() or runtime_stat.st_mode & 0o022:
            raise RuntimeError(
                "The Stylix runtime must be owned by the service user and not "
                "writable by group or others"
            )
        git = shutil.which("git") or "/usr/bin/git"
        return [str(NIX_PORTABLE), "nix"], {"NP_RUNTIME": "proot", "NP_GIT": git}

    nix_command = NIX_COMMAND or "nix"
    nix_path = Path(nix_command).expanduser()
    if not nix_path.is_absolute():
        located = shutil.which(nix_command)
        if located is None:
            raise RuntimeError(
                "Nix is required for Stylix palette generation. Install Nix or set "
                "PALETTE_NIX_PORTABLE to a nix-portable binary."
            )
        nix_path = Path(located)
    try:
        resolved = nix_path.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("The configured Nix command is missing") from error
    if not resolved.is_file():
        raise RuntimeError("The configured Nix command is not a file")
    return [str(resolved)], {}


def stylix_command(
    polarity: str,
    wallpaper: Path,
    output: Path,
) -> tuple[list[str], dict[str, str]]:
    runner, environment = nix_runner()
    return (
        runner
        + [
            "--extra-experimental-features",
            "nix-command flakes",
            "run",
            GENERATOR_FLAKE,
            "--",
            polarity,
            str(wallpaper),
            str(output),
        ],
        environment,
    )


def validate_runtime() -> None:
    nix_runner()


def start_generation(
    polarity: str = "dark",
    supplied_image_url: str | None = None,
) -> dict[str, object]:
    if polarity not in {"dark", "light"}:
        raise ValueError("Invalid palette polarity")
    validate_runtime()

    if supplied_image_url is None:
        with request(API_URL, 30) as response:
            posts = read_limited_json(response)
        if not isinstance(posts, list) or not posts or not isinstance(posts[0], dict):
            raise ValueError("Wallpaper provider returned an invalid response")

        post = posts[0]
        post_id = post.get("id")
        if not isinstance(post_id, int) or post_id <= 0:
            raise ValueError("Wallpaper provider returned an invalid post ID")
        image_url = validate_remote_url(post.get("file_url"))
        tags = str(post.get("tags", ""))[:4096]
        post_url = f"https://konachan.net/post/show/{post_id}"
        source_label = "view on konachan"
    else:
        post_id = secrets.randbelow(1_000_000_000)
        image_url = supplied_image_url
        tags = "custom image"
        post_url = supplied_image_url
        source_label = "open image source"

    HISTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=HISTORY, prefix=".wallpaper-")
    os.close(descriptor)
    temporary_wallpaper = Path(temporary_name)
    try:
        if supplied_image_url is None:
            content_type = download_limited(image_url, temporary_wallpaper)
        else:
            content_type, image_url = download_public_image(
                image_url, temporary_wallpaper
            )
            post_url = image_url
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
        "tags": tags,
        "post_url": post_url,
        "source_label": source_label,
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
    with active_jobs_lock:
        active_jobs[record_id] = job

    threading.Thread(
        target=finish_generation,
        args=(job,),
        name=f"stylix-{record_id}",
        daemon=True,
    ).start()
    return public_record(metadata)


def regenerate_record(record_id: str, polarity: str) -> dict[str, object]:
    if polarity not in {"dark", "light"} or not RECORD_ID.fullmatch(record_id):
        raise ValueError("Invalid regeneration request")
    validate_runtime()
    source_dir = HISTORY / record_id
    source_wallpaper = source_dir / "wallpaper"
    source_metadata_file = source_dir / "metadata.json"
    if (
        not source_wallpaper.is_file()
        or source_wallpaper.is_symlink()
        or not source_metadata_file.is_file()
        or source_metadata_file.is_symlink()
    ):
        raise ValueError("Wallpaper record was not found")
    source = load_record(source_metadata_file)

    created_at = datetime.now(timezone.utc)
    new_id = (
        f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{secrets.randbelow(1_000_000_000)}"
    )
    record_dir = HISTORY / new_id
    record_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    wallpaper = record_dir / "wallpaper"
    try:
        os.link(source_wallpaper, wallpaper)
    except OSError:
        shutil.copyfile(source_wallpaper, wallpaper)
    os.chmod(wallpaper, 0o600)

    content_type = source.get("content_type", "image/jpeg")
    if content_type not in IMAGE_TYPES.values():
        content_type = validate_image(wallpaper)
    metadata = {
        "id": new_id,
        "image": f"/history/{new_id}/wallpaper",
        "tags": str(source.get("tags", ""))[:4096],
        "post_url": source.get("post_url", ""),
        "source_label": source.get("source_label", "view on konachan"),
        "image_url": source.get("image_url", ""),
        "sha256": source.get("sha256") or nix_sha256(wallpaper),
        "created_at": created_at.isoformat(),
        "content_type": content_type,
        "polarity": polarity,
        "status": "generating",
    }
    atomic_write_json(record_dir / "metadata.json", metadata)

    job: dict[str, object] = {
        "id": new_id,
        "polarity": polarity,
        "cancel": threading.Event(),
        "process": None,
    }
    with active_jobs_lock:
        active_jobs[new_id] = job
    threading.Thread(
        target=finish_generation,
        args=(job,),
        name=f"stylix-{new_id}",
        daemon=True,
    ).start()
    return public_record(metadata)


def finish_generation(job: dict[str, object]) -> None:
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

    try:
        command, runtime_environment = stylix_command(
            str(job["polarity"]),
            record_dir / "wallpaper",
            temporary_palette,
        )
        environment.update(runtime_environment)
        if cancel.is_set():
            raise InterruptedError("Palette generation skipped")

        process = subprocess.Popen(
            command,
            cwd=DATA_DIR,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with active_jobs_lock:
            job["process"] = process
            should_cancel = cancel.is_set()
        if should_cancel:
            os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=240)
        if cancel.is_set():
            raise InterruptedError("Palette generation skipped")
        if process.returncode:
            raise RuntimeError(
                stderr.strip() or stdout.strip() or "Stylix generation failed"
            )

        if temporary_palette.stat().st_size > 64 * 1024:
            raise RuntimeError("Stylix returned an oversized palette")
        palette = json.loads(temporary_palette.read_text(encoding="utf-8"))
        if (
            not isinstance(palette, dict)
            or set(palette) != {f"base{i:02X}" for i in range(16)}
            or not all(
                isinstance(value, str) and HEX_COLOR.fullmatch(value)
                for value in palette.values()
            )
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
        print(
            f"[palette] generation {record_id} failed: {type(error).__name__}: {error}"
        )
        try:
            metadata = load_record(metadata_file)
            status = "skipped" if cancel.is_set() else "error"
            message = (
                "Palette generation skipped"
                if status == "skipped"
                else "Palette generation failed"
            )
            metadata.update({"status": status, "error": message})
            atomic_write_json(metadata_file, metadata)
        except (OSError, ValueError):
            pass
    finally:
        with active_jobs_lock:
            active_jobs.pop(record_id, None)
        try:
            prune_history()
        except OSError as error:
            print(f"[palette] history cleanup failed: {error}")
        generation_slots.release()


def cancel_active_generation(record_id: str | None = None) -> bool:
    with active_jobs_lock:
        if record_id is None:
            job = next(reversed(active_jobs.values()), None)
        else:
            job = active_jobs.get(record_id)
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


def remove_interrupted_generations() -> None:
    if not HISTORY.is_dir():
        return
    for metadata_file in HISTORY.glob("*/metadata.json"):
        try:
            if (
                not RECORD_ID.fullmatch(metadata_file.parent.name)
                or metadata_file.is_symlink()
            ):
                continue
            record = load_record(metadata_file)
            if record.get("status") != "generating":
                continue
            shutil.rmtree(metadata_file.parent)
        except (OSError, ValueError):
            continue


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
            status = record.get("status", "ready")
            if status not in {"generating", "ready"}:
                continue
            if status == "ready" and not record.get("palette"):
                continue
            wallpaper = metadata_file.parent / "wallpaper"
            changed = False
            if (
                wallpaper.is_file()
                and not wallpaper.is_symlink()
                and not record.get("sha256")
            ):
                record["sha256"] = nix_sha256(wallpaper)
                changed = True
            if changed:
                atomic_write_json(metadata_file, record)
            records.append(public_record(record))
        except (OSError, ValueError):
            continue
    if MAX_HISTORY_ITEMS > 0:
        return records[:MAX_HISTORY_ITEMS]
    return records


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
                if (
                    not buckets[stale_key]
                    or buckets[stale_key][-1] <= now - RATE_LIMIT_WINDOW
                ):
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
        # History paths contain a unique record ID and their wallpaper never changes.
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
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
            self.send_json(
                {"error": "Cross-origin request denied"}, HTTPStatus.FORBIDDEN
            )
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_json(
                {"error": "Chunked request bodies are not allowed"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": "Invalid content length"}, HTTPStatus.BAD_REQUEST)
            return
        if not 0 <= content_length <= 4096:
            self.send_json(
                {"error": "Request body is too large"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        supplied_image_url = None
        source_record_id = None
        if content_length:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                self.send_json(
                    {"error": "Expected application/json"},
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
                return
            try:
                payload = json.loads(self.rfile.read(content_length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_json({"error": "Invalid JSON body"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(payload, dict):
                self.send_json(
                    {"error": "Expected a JSON object"}, HTTPStatus.BAD_REQUEST
                )
                return
            if set(payload) == {"image_url"} and isinstance(payload["image_url"], str):
                supplied_image_url = payload["image_url"].strip()
                try:
                    resolve_public_https_url(supplied_image_url)
                except ValueError:
                    self.send_json(
                        {"error": "Image URL must be public HTTPS"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
            elif set(payload) == {"record_id"} and isinstance(
                payload["record_id"], str
            ):
                source_record_id = payload["record_id"]
                if generation_status(source_record_id) is None:
                    self.send_json(
                        {"error": "Wallpaper record was not found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
            else:
                self.send_json(
                    {"error": "Expected one image_url or record_id string"},
                    HTTPStatus.BAD_REQUEST,
                )
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
            self.send_json(
                {"error": "Invalid palette polarity"}, HTTPStatus.BAD_REQUEST
            )
            return
        polarity = polarity_values[0]
        skip_values = parameters.get("skip", [])
        if len(skip_values) > 1 or (
            skip_values
            and skip_values[0] != "1"
            and not RECORD_ID.fullmatch(skip_values[0])
        ):
            self.send_json(
                {"error": "Invalid generation to skip"}, HTTPStatus.BAD_REQUEST
            )
            return
        skip_record_id = skip_values[0] if skip_values else None
        if skip_record_id is not None:
            cancel_active_generation(
                None if skip_record_id == "1" else skip_record_id
            )
            acquired = generation_slots.acquire(timeout=15)
        else:
            acquired = generation_slots.acquire(blocking=False)
        if not acquired:
            self.send_json(
                {"error": "Four palettes are already being generated"},
                HTTPStatus.CONFLICT,
            )
            return
        try:
            if source_record_id is not None:
                result = regenerate_record(source_record_id, polarity)
            else:
                result = start_generation(polarity, supplied_image_url)
            self.send_json(result, 202)
        except Exception as error:
            generation_slots.release()
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
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    HISTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    remove_interrupted_generations()
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
