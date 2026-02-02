{
  description = "Audio recording setup for Korg NTS-1 and Android mic";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python312.withPackages (ps: [
        ps.sounddevice
        ps.numpy
        ps.scipy  # for wav file writing
      ]);
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          python
          pkgs.scrcpy
          pkgs.android-tools  # adb
          pkgs.pipewire       # pw-record, pw-link, pw-cli
          pkgs.pulseaudio     # pactl
        ];

        shellHook = ''
          echo "Record Music Dev Shell"
          echo "  - scrcpy $(scrcpy --version 2>&1 | head -1)"
          echo "  - adb $(adb version 2>&1 | head -1)"
          echo "  - python $(python --version)"
          echo "  - pw-record, pw-link, pactl available"
          echo ""
          echo "Usage:"
          echo "  ./monitor.py             # Monitor all sources"
          echo "  ./record.py -d 30        # Record for 30 seconds"
          echo "  ./record.py --list       # List audio devices"
          echo ""
        '';
      };
    };
}
