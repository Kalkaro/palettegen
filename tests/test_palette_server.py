import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "palette_server.py"
ENV_KEYS = (
    "PALETTE_DATA_DIR",
    "PALETTE_MAX_HISTORY",
    "PALETTE_NIX",
    "PALETTE_NIX_PORTABLE",
    "XDG_DATA_HOME",
)


def install_dependency_stubs() -> None:
    pil = types.ModuleType("PIL")

    class FakeImage:
        DecompressionBombWarning = Warning
        DecompressionBombError = Exception
        MAX_IMAGE_PIXELS = None

    pil.Image = FakeImage
    pil.UnidentifiedImageError = ValueError
    sys.modules.setdefault("PIL", pil)

    urllib3 = types.ModuleType("urllib3")

    class FakeHTTPSConnectionPool:
        pass

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    urllib3.HTTPSConnectionPool = FakeHTTPSConnectionPool
    urllib3.Timeout = FakeTimeout
    sys.modules.setdefault("urllib3", urllib3)


def make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o700)


def load_server(env: dict[str, str]):
    install_dependency_stubs()
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(env)

        spec = importlib.util.spec_from_file_location("palette_server_under_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class PaletteServerRuntimeTests(unittest.TestCase):
    def test_wallpaper_request_requires_2560_by_1440_or_larger(self):
        server = load_server({})
        parameters = parse_qs(urlparse(server.API_URL).query)

        self.assertEqual(parameters["limit"], ["1"])
        self.assertEqual(
            parameters["tags"],
            ["order:random rating:safe width:>=2560 height:>=1440"],
        )

    def test_data_dir_controls_default_history_location(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server({"PALETTE_DATA_DIR": directory})

            self.assertEqual(
                server.HISTORY,
                Path(directory).resolve(),
            )

    def test_default_data_and_history_share_one_xdg_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server({"XDG_DATA_HOME": directory})
            expected = Path(directory).resolve() / "palette-generator"

            self.assertEqual(server.DATA_DIR, expected)
            self.assertEqual(server.HISTORY, expected)

    def test_stylix_command_uses_configured_system_nix(self):
        with tempfile.TemporaryDirectory() as directory:
            nix = Path(directory) / "nix"
            make_executable(nix)

            server = load_server({"PALETTE_NIX": str(nix)})
            command, environment = server.stylix_command(
                "dark",
                Path("/tmp/wallpaper"),
                Path("/tmp/palette.json"),
            )

        self.assertEqual(
            command[:5],
            [
                str(nix.resolve()),
                "--extra-experimental-features",
                "nix-command flakes",
                "run",
                server.GENERATOR_FLAKE,
            ],
        )
        self.assertEqual(
            command[-4:],
            ["--", "dark", "/tmp/wallpaper", "/tmp/palette.json"],
        )
        self.assertEqual(environment, {})

    def test_stylix_command_uses_nix_portable_when_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            portable = Path(directory) / "nix-portable"
            make_executable(portable)

            server = load_server(
                {
                    "PALETTE_NIX": "/missing/nix",
                    "PALETTE_NIX_PORTABLE": str(portable),
                }
            )
            command, environment = server.stylix_command(
                "light",
                Path("/tmp/wallpaper"),
                Path("/tmp/palette.json"),
            )

        self.assertEqual(
            command[:6],
            [
                str(portable.resolve()),
                "nix",
                "--extra-experimental-features",
                "nix-command flakes",
                "run",
                server.GENERATOR_FLAKE,
            ],
        )
        self.assertEqual(
            command[-4:],
            ["--", "light", "/tmp/wallpaper", "/tmp/palette.json"],
        )
        self.assertEqual(environment["NP_RUNTIME"], "proot")
        self.assertIn("NP_GIT", environment)

    def test_missing_nix_runtime_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server({"PALETTE_NIX": str(Path(directory) / "missing")})
            with self.assertRaisesRegex(RuntimeError, "missing"):
                server.validate_runtime()

    def test_public_record_strips_internal_content_type(self):
        server = load_server({})
        record = server.public_record({"id": "record", "content_type": "image/jpeg"})

        self.assertEqual(record, {"id": "record"})

    def test_history_includes_generating_records(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server({"PALETTE_DATA_DIR": directory})
            pending_id = "20260813T120000000000Z-1"
            ready_id = "20260813T110000000000Z-2"
            pending_dir = server.HISTORY / pending_id
            ready_dir = server.HISTORY / ready_id
            pending_dir.mkdir()
            ready_dir.mkdir()
            server.atomic_write_json(
                pending_dir / "metadata.json",
                {"id": pending_id, "status": "generating", "sha256": "pending"},
            )
            server.atomic_write_json(
                ready_dir / "metadata.json",
                {
                    "id": ready_id,
                    "status": "ready",
                    "sha256": "ready",
                    "palette": {"base00": "18191c"},
                },
            )

            records = server.history()

        self.assertEqual(
            [(record["id"], record["status"]) for record in records],
            [(pending_id, "generating"), (ready_id, "ready")],
        )

    def test_startup_cleanup_removes_an_interrupted_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server({"PALETTE_DATA_DIR": directory})
            record_id = "20260813T120000000000Z-1"
            record_dir = server.HISTORY / record_id
            record_dir.mkdir()
            server.atomic_write_json(
                record_dir / "metadata.json",
                {"id": record_id, "status": "generating", "sha256": "pending"},
            )

            server.remove_interrupted_generations()

            self.assertFalse(record_dir.exists())
            self.assertIsNone(server.generation_status(record_id))
            self.assertEqual(server.history(), [])

    def test_cancel_active_generation_targets_one_job(self):
        server = load_server({})
        first_cancel = threading.Event()
        second_cancel = threading.Event()
        first_id = "20260813T120000000000Z-1"
        second_id = "20260813T120001000000Z-2"
        server.active_jobs.update(
            {
                first_id: {"id": first_id, "cancel": first_cancel, "process": None},
                second_id: {
                    "id": second_id,
                    "cancel": second_cancel,
                    "process": None,
                },
            }
        )

        self.assertTrue(server.cancel_active_generation(first_id))
        self.assertTrue(first_cancel.is_set())
        self.assertFalse(second_cancel.is_set())
        self.assertTrue(server.cancel_active_generation())
        self.assertTrue(second_cancel.is_set())

    def test_clear_history_removes_completed_records_and_keeps_active_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server({"PALETTE_DATA_DIR": directory})
            ready_id = "20260813T120002000000Z-1"
            active_id = "20260813T120001000000Z-2"
            error_id = "20260813T120000000000Z-3"
            for record_id, status in (
                (ready_id, "ready"),
                (active_id, "generating"),
                (error_id, "error"),
            ):
                record_dir = server.HISTORY / record_id
                record_dir.mkdir()
                server.atomic_write_json(
                    record_dir / "metadata.json",
                    {"id": record_id, "status": status},
                )
            server.active_jobs[active_id] = {
                "id": active_id,
                "cancel": threading.Event(),
                "process": None,
            }

            removed = server.clear_history()

            self.assertCountEqual(removed, [ready_id, error_id])
            self.assertFalse((server.HISTORY / ready_id).exists())
            self.assertTrue((server.HISTORY / active_id).is_dir())
            self.assertFalse((server.HISTORY / error_id).exists())

    def test_generation_slots_allow_two_jobs(self):
        server = load_server({})

        self.assertTrue(server.generation_slots.acquire(blocking=False))
        self.assertTrue(server.generation_slots.acquire(blocking=False))
        self.assertFalse(server.generation_slots.acquire(blocking=False))

        server.generation_slots.release()
        server.generation_slots.release()

    def test_pruning_preserves_an_active_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server(
                {
                    "PALETTE_DATA_DIR": directory,
                    "PALETTE_MAX_HISTORY": "1",
                }
            )
            newest_id = "20260813T120002000000Z-1"
            active_id = "20260813T120001000000Z-2"
            oldest_id = "20260813T120000000000Z-3"
            for record_id in (newest_id, active_id, oldest_id):
                record_dir = server.HISTORY / record_id
                record_dir.mkdir()
                (record_dir / "wallpaper").write_bytes(b"wallpaper")
            server.active_jobs[active_id] = {
                "id": active_id,
                "cancel": threading.Event(),
                "process": None,
            }

            server.prune_history()

            self.assertTrue((server.HISTORY / newest_id).is_dir())
            self.assertTrue((server.HISTORY / active_id).is_dir())
            self.assertFalse((server.HISTORY / oldest_id).exists())


if __name__ == "__main__":
    unittest.main()
