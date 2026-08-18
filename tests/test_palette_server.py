import importlib.util
import json
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
    "PALETTE_MATUGEN",
    "PALETTE_NIX",
    "PALETTE_NIX_PORTABLE",
    "PALETTE_PYWAL",
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


def matugen_payload() -> dict[str, object]:
    role_colors = {
        "background": "000000",
        "surface_container_low": "111111",
        "surface_container_high": "222222",
        "outline": "333333",
        "on_surface_variant": "444444",
        "on_surface": "555555",
        "on_background": "666666",
        "inverse_surface": "777777",
        "error": "888888",
        "tertiary": "999999",
        "secondary": "aaaaaa",
        "primary": "bbbbbb",
        "error_container": "cccccc",
    }
    return {
        "colors": {
            role: {"default": {"color": color}}
            for role, color in role_colors.items()
        }
    }


def pywal_payload() -> dict[str, object]:
    return {
        "special": {"foreground": "#555555"},
        "colors": {
            f"color{index}": f"#{index:02x}{index:02x}{index:02x}"
            for index in range(16)
        },
    }


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

    def test_matugen_command_uses_configured_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            matugen = Path(directory) / "matugen"
            make_executable(matugen)

            server = load_server({"PALETTE_MATUGEN": str(matugen)})
            command, environment = server.matugen_command(
                "light",
                Path("/tmp/wallpaper.jpg"),
            )

        self.assertEqual(
            command,
            [
                str(matugen.resolve()),
                "image",
                "/tmp/wallpaper.jpg",
                "--mode",
                "light",
                "--type",
                "scheme-tonal-spot",
                "--source-color-index",
                "0",
                "--json",
                "strip",
                "--dry-run",
                "--quiet",
            ],
        )
        self.assertEqual(environment, {})

    def test_matugen_runtime_does_not_require_nix(self):
        with tempfile.TemporaryDirectory() as directory:
            matugen = Path(directory) / "matugen"
            make_executable(matugen)
            server = load_server(
                {
                    "PALETTE_MATUGEN": str(matugen),
                    "PALETTE_NIX": str(Path(directory) / "missing-nix"),
                }
            )

            server.validate_runtime("matugen")
            server.validate_available_runtime()

    def test_matugen_material_roles_are_mapped_to_base16(self):
        server = load_server({})

        self.assertEqual(
            server.matugen_palette(matugen_payload()),
            {
                "base00": "000000",
                "base01": "111111",
                "base02": "222222",
                "base03": "333333",
                "base04": "444444",
                "base05": "555555",
                "base06": "666666",
                "base07": "777777",
                "base08": "888888",
                "base09": "999999",
                "base0A": "aaaaaa",
                "base0B": "bbbbbb",
                "base0C": "999999",
                "base0D": "bbbbbb",
                "base0E": "aaaaaa",
                "base0F": "cccccc",
            },
        )

    def test_matugen_generation_writes_a_ready_palette_without_nix(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            matugen = Path(directory) / "matugen"
            matugen.write_text(
                "#!/bin/sh\n"
                'case "$2" in *.jpg) ;; *) exit 2 ;; esac\n'
                f"printf '%s' '{json.dumps(matugen_payload())}'\n",
                encoding="utf-8",
            )
            matugen.chmod(0o700)
            server = load_server(
                {
                    "PALETTE_DATA_DIR": str(data_dir),
                    "PALETTE_MATUGEN": str(matugen),
                    "PALETTE_NIX": str(Path(directory) / "missing-nix"),
                }
            )
            record_id = "20260814T120000000000Z-1"
            record_dir = server.HISTORY / record_id
            record_dir.mkdir(parents=True)
            (record_dir / "wallpaper").write_bytes(b"wallpaper")
            server.atomic_write_json(
                record_dir / "metadata.json",
                {
                    "id": record_id,
                    "content_type": "image/jpeg",
                    "polarity": "dark",
                    "generator": "matugen",
                    "status": "generating",
                },
            )
            job = {
                "id": record_id,
                "polarity": "dark",
                "generator": "matugen",
                "cancel": threading.Event(),
                "process": None,
            }
            server.active_jobs[record_id] = job
            self.assertTrue(server.generation_slots.acquire(blocking=False))

            server.finish_generation(job)

            metadata = server.load_record(record_dir / "metadata.json")
            palette = server.load_record(record_dir / "palette.json")
            self.assertEqual(metadata["status"], "ready")
            self.assertEqual(metadata["generator"], "matugen")
            self.assertEqual(palette, server.matugen_palette(matugen_payload()))
            self.assertEqual(list(record_dir.glob(".matugen-*")), [])

    def test_pywal_command_uses_configured_runtime_and_light_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            pywal = Path(directory) / "wal"
            make_executable(pywal)

            server = load_server({"PALETTE_PYWAL": str(pywal)})
            command, environment = server.pywal_command(
                "light",
                Path("/tmp/wallpaper.jpg"),
                Path("/tmp/output"),
            )

        self.assertEqual(
            command,
            [
                str(pywal.resolve()),
                "--cols16",
                "--contrast",
                "1.5",
                "--saturate",
                "0.2",
                "-i",
                "/tmp/wallpaper.jpg",
                "--out-dir",
                "/tmp/output",
                "-l",
                "-n",
                "-s",
                "-t",
                "-e",
                "-q",
            ],
        )
        self.assertEqual(environment, {})

    def test_pywal_colors_are_mapped_to_base16(self):
        server = load_server({})

        self.assertEqual(
            server.pywal_palette(pywal_payload()),
            {
                "base00": "000000",
                "base01": "000000",
                "base02": "080808",
                "base03": "080808",
                "base04": "070707",
                "base05": "555555",
                "base06": "0f0f0f",
                "base07": "0f0f0f",
                "base08": "010101",
                "base09": "090909",
                "base0A": "030303",
                "base0B": "020202",
                "base0C": "060606",
                "base0D": "040404",
                "base0E": "050505",
                "base0F": "0d0d0d",
            },
        )

    def test_pywal_runtime_does_not_require_nix(self):
        with tempfile.TemporaryDirectory() as directory:
            pywal = Path(directory) / "wal"
            make_executable(pywal)
            server = load_server(
                {
                    "PALETTE_MATUGEN": str(Path(directory) / "missing-matugen"),
                    "PALETTE_NIX": str(Path(directory) / "missing-nix"),
                    "PALETTE_PYWAL": str(pywal),
                }
            )

            server.validate_runtime("pywal16")
            server.validate_available_runtime()

    def test_pywal_generation_writes_a_ready_palette_without_nix(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            pywal = Path(directory) / "wal"
            pywal.write_text(
                "#!/bin/sh\n"
                'case "$7" in *.png) ;; *) exit 2 ;; esac\n'
                'mkdir -p "$9"\n'
                f"printf '%s' '{json.dumps(pywal_payload())}' > \"$9/colors.json\"\n",
                encoding="utf-8",
            )
            pywal.chmod(0o700)
            server = load_server(
                {
                    "PALETTE_DATA_DIR": str(data_dir),
                    "PALETTE_PYWAL": str(pywal),
                    "PALETTE_NIX": str(Path(directory) / "missing-nix"),
                }
            )
            record_id = "20260814T120000000000Z-2"
            record_dir = server.HISTORY / record_id
            record_dir.mkdir(parents=True)
            (record_dir / "wallpaper").write_bytes(b"wallpaper")
            server.atomic_write_json(
                record_dir / "metadata.json",
                {
                    "id": record_id,
                    "content_type": "image/png",
                    "polarity": "dark",
                    "generator": "pywal16",
                    "status": "generating",
                },
            )
            job = {
                "id": record_id,
                "polarity": "dark",
                "generator": "pywal16",
                "cancel": threading.Event(),
                "process": None,
            }
            server.active_jobs[record_id] = job
            self.assertTrue(server.generation_slots.acquire(blocking=False))

            server.finish_generation(job)

            metadata = server.load_record(record_dir / "metadata.json")
            palette = server.load_record(record_dir / "palette.json")
            self.assertEqual(metadata["status"], "ready")
            self.assertEqual(metadata["generator"], "pywal16")
            self.assertEqual(palette, server.pywal_palette(pywal_payload()))
            self.assertEqual(list(record_dir.glob(".pywal-*")), [])

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

    def test_history_page_returns_ten_then_the_eleventh_record(self):
        with tempfile.TemporaryDirectory() as directory:
            server = load_server({"PALETTE_DATA_DIR": directory})
            record_ids = [
                f"20260813T12{minute:02d}00000000Z-{minute}"
                for minute in range(12)
            ]
            for record_id in record_ids:
                record_dir = server.HISTORY / record_id
                record_dir.mkdir()
                server.atomic_write_json(
                    record_dir / "metadata.json",
                    {
                        "id": record_id,
                        "status": "ready",
                        "sha256": record_id,
                        "palette": {"base00": "18191c"},
                    },
                )

            first_page = server.history_page(10)
            second_page = server.history_page(1, first_page["next_cursor"])

        self.assertEqual(
            [record["id"] for record in first_page["history"]],
            list(reversed(record_ids))[:10],
        )
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["next_cursor"], record_ids[2])
        self.assertEqual(
            [record["id"] for record in second_page["history"]],
            [record_ids[1]],
        )
        self.assertTrue(second_page["has_more"])
        self.assertEqual(second_page["next_cursor"], record_ids[1])

    def test_history_page_rejects_invalid_limits_and_cursors(self):
        server = load_server({})

        for limit in (0, 51):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    server.history_page(limit)
        with self.assertRaises(ValueError):
            server.history_page(10, "../../history")

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

    def test_generation_slots_allow_four_jobs(self):
        server = load_server({})

        for _ in range(4):
            self.assertTrue(server.generation_slots.acquire(blocking=False))
        self.assertFalse(server.generation_slots.acquire(blocking=False))

        for _ in range(4):
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
