{
  description = "MonkeyTester-Plugin-AI: the vision-model plugin for MonkeyTester (Ollama / remote server / GitHub Copilot)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # Standard library only. `setup` downloads its own pinned Ollama into
        # .runtime/, so Ollama is deliberately not a Nix input.
        python = pkgs.python3;

        # .runtime/ (Ollama, models, settings, sign-ins) defaults to the checkout,
        # never the read-only Nix store, so these run from the repo root
        # (or set VTAI_RUNTIME_DIR).
        requireCheckout = ''
          if [ ! -f bin/monkeytester-ai ] || [ ! -d monkeytester_ai ]; then
            echo "Run this from the MonkeyTester-Plugin-AI checkout (the directory with bin/monkeytester-ai)." >&2
            exit 1
          fi
        '';

        # serve/setup need macOS on Apple silicon (the CLI says so elsewhere);
        # status, ask and the tests work anywhere.
        cli = pkgs.writeShellApplication {
          name = "monkeytester-ai";
          runtimeInputs = [ python ];
          text = ''
            ${requireCheckout}
            exec python3 bin/monkeytester-ai "$@"
          '';
        };

        # Same command as .github/workflows/test.yml.
        tests = pkgs.writeShellApplication {
          name = "monkeytester-ai-tests";
          runtimeInputs = [ python ];
          text = ''
            ${requireCheckout}
            exec python3 -m unittest discover tests "$@"
          '';
        };
      in {
        packages.default = cli;
        packages.monkeytester-ai = cli;
        packages.tests = tests;

        # nix run . -- setup | serve [--lan] | status | ask <img> "<q>" | ...
        # nix run .#tests     -> unit tests (fake Ollama/GitHub/remote; any OS)
        apps.default = { type = "app"; program = "${cli}/bin/monkeytester-ai"; };
        apps.tests = { type = "app"; program = "${tests}/bin/monkeytester-ai-tests"; };

        devShells.default = pkgs.mkShell { packages = [ python ]; };
      });
}
