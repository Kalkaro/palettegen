{
  description = "Stylix palette generator web app";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          lib = pkgs.lib;
          python = pkgs.python3.withPackages (
            pythonPackages: with pythonPackages; [
              pillow
              urllib3
            ]
          );
          runtimePath = lib.makeBinPath [
            pkgs.git
            pkgs.nix
          ];
          nix = "${pkgs.nix}/bin/nix";
        in
        {
          default = pkgs.stdenvNoCC.mkDerivation {
            pname = "palette-generator";
            version = "0.1.0";

            src = lib.cleanSourceWith {
              src = ./.;
              filter =
                path: type:
                let
                  relative = lib.removePrefix "${toString ./.}/" (toString path);
                in
                type == "directory"
                || relative == "palette_server.py"
                || relative == "palette-showcase.html";
            };

            dontConfigure = true;
            dontBuild = true;

            installPhase = ''
              runHook preInstall

              install -Dm644 palette_server.py "$out/share/palette-generator/palette_server.py"
              install -Dm644 palette-showcase.html "$out/share/palette-generator/palette-showcase.html"

              mkdir -p "$out/bin"
              cat > "$out/bin/palette-generator" <<'EOF'
              #!@shell@
              set -eu

              if [ -z "''${PALETTE_DATA_DIR:-}" ]; then
                if [ -n "''${XDG_DATA_HOME:-}" ]; then
                  PALETTE_DATA_DIR="$XDG_DATA_HOME/palette-generator"
                elif [ -n "''${HOME:-}" ]; then
                  PALETTE_DATA_DIR="$HOME/.local/share/palette-generator"
                else
                  PALETTE_DATA_DIR="''${TMPDIR:-/tmp}/palette-generator"
                fi
                export PALETTE_DATA_DIR
              fi

              export PATH="@runtimePath@:''${PATH:-}"
              export PALETTE_NIX="''${PALETTE_NIX:-@nix@}"
              export PYTHONNOUSERSITE=1
              exec @python@ @out@/share/palette-generator/palette_server.py "$@"
              EOF
              substituteInPlace "$out/bin/palette-generator" \
                --replace-fail @shell@ "${pkgs.runtimeShell}" \
                --replace-fail @runtimePath@ "${runtimePath}" \
                --replace-fail @nix@ "${nix}" \
                --replace-fail @python@ "${python}/bin/python3" \
                --replace-fail @out@ "$out"
              chmod +x "$out/bin/palette-generator"

              runHook postInstall
            '';
          };
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/palette-generator";
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3.withPackages (
            pythonPackages: with pythonPackages; [
              pillow
              urllib3
            ]
          );
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.git
              pkgs.nix
            ];

            NIX_CONFIG = "experimental-features = nix-command flakes";
          };
        }
      );
    };
}
