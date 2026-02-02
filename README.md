# music-recorder3000

Record audio from multiple sources simultaneously on Linux using PipeWire.

Built for recording a **Korg NTS-1** synth via USB audio interface and an **Android phone microphone** via scrcpy - but works with any PipeWire audio sources.

## Features

- **Multi-source recording**: Capture from USB audio devices and Android phone mic simultaneously
- **Virtual mixing**: Routes all sources through a PipeWire virtual sink for combined monitoring and recording
- **Smooth volume ramping**: Gradual fade-in on start and fade-out before disconnect - no harsh audio pops or clicks from abrupt cutoffs. If your brain doesn't like sounds going from 1 to 0 instantly, this is for you.
- **Automatic cleanup**: All PipeWire links and virtual devices are properly removed on exit
- **Nix flake**: Reproducible dev environment with all dependencies

## Requirements

- NixOS / Nix with flakes
- PipeWire (standard on most modern Linux)
- Android phone with USB debugging enabled (for mic capture)
- USB audio interface (for synth/instrument input)

## Usage

```bash
# Enter the dev shell
nix develop

# Connect your Android phone via USB and enable USB debugging

# Monitor all sources (hear them through your speakers/headphones)
./monitor.py

# Record for 30 seconds
./record.py -d 30

# Record only the synth
./record.py --synth-only -d 60

# Record only the Android mic
./record.py --mic-only -d 60

# List available audio devices
./record.py --list
```

## How it works

```
┌─────────────────┐
│ Korg NTS-1      │──► USB Audio Interface ──┐
└─────────────────┘                          │
                                             ▼
                                    ┌────────────────┐
                                    │  Virtual Sink  │──► Your Headphones
                                    │  (record_mix)  │
                                    └────────────────┘
┌─────────────────┐                          ▲        │
│ Android Phone   │──► scrcpy (mic audio) ───┘        │
└─────────────────┘                                   ▼
                                              ┌──────────────┐
                                              │  Recording   │
                                              │  (.wav file) │
                                              └──────────────┘
```

1. Creates a PipeWire virtual sink for mixing
2. Starts scrcpy to stream Android mic audio over USB
3. Connects USB audio capture and scrcpy to the virtual sink
4. Routes the virtual sink to your default output (so you can monitor)
5. Records from the virtual sink's monitor

## Files

- `audio_setup.py` - PipeWire routing, scrcpy management, volume ramping
- `monitor.py` - Live monitoring of all sources
- `record.py` - Record to WAV files
- `flake.nix` - Nix dev shell with dependencies

## Why volume ramping?

Audio that cuts off abruptly (going from full volume to silence in one sample) creates a harsh click or pop. This is jarring and can be uncomfortable, especially on headphones.

This tool fades volume smoothly over 0.5 seconds when starting and stopping - your ears will thank you.

## See also

- [mpv-gradual-pause](https://github.com/gui-wf/mpv-gradual-pause) - Same concept for mpv: smooth audio fade effects when pausing/unpausing video
