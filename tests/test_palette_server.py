import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "palette_server.py"
ENV_KEYS = (
    "PALETTE_DATA_DIR",
    "PALETTE_HISTORY_DIR",
    "PALETTE_NIX",
    "PALETTE_NIX_PORTABLE",
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
                Path(directory).resolve() / "palette-history",
            )

    def test_history_dir_overrides_data_dir(self):
        with tempfile.TemporaryDirectory() as data_directory:
            with tempfile.TemporaryDirectory() as history_directory:
                server = load_server(
                    {
                        "PALETTE_DATA_DIR": data_directory,
                        "PALETTE_HISTORY_DIR": history_directory,
                    }
                )

                self.assertEqual(server.HISTORY, Path(history_directory).resolve())

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


if __name__ == "__main__":
    unittest.main()
