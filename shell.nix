{ pkgs ? import <nixpkgs> { } }:

let
  python = pkgs.python3.withPackages (pythonPackages: with pythonPackages; [
    pillow
    urllib3
  ]);
in
pkgs.mkShell {
  packages = [
    python
    pkgs.git
    pkgs.matugen
    pkgs.nix
    pkgs.pywal16
  ];

  NIX_CONFIG = "experimental-features = nix-command flakes";

  shellHook = ''
    echo "Palette server shell ready. Run: python3 palette_server.py"
  '';
}
