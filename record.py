#!/usr/bin/env python3
"""
Record audio from multiple sources simultaneously:
- Korg NTS-1 via USB audio interface
- Android phone mic via scrcpy
"""

import argparse
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from audio_setup import AudioSetup, record_with_pw_record

# Global flag for stopping recording
stop_recording = threading.Event()


def record_with_pw(
    targets: dict[str, str],
    duration: float,
    output_dir: Path,
) -> dict[str, Path]:
    """Record from targets using pw-record."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    processes: dict[str, subprocess.Popen] = {}
    output_files: dict[str, Path] = {}

    # Set up signal handler
    def signal_handler(sig, frame):
        print("\n  Stopping recording...")
        stop_recording.set()

    signal.signal(signal.SIGINT, signal_handler)
    stop_recording.clear()

    # Start all recordings
    for name, target in targets.items():
        if target:
            output_path = output_dir / f"{timestamp}_{name}.wav"
            output_files[name] = output_path
            print(f"  Recording {name} from '{target}' -> {output_path}")
            processes[name] = record_with_pw_record(target, output_path)

    if not processes:
        print("No targets to record!")
        return {}

    # Wait for duration or Ctrl+C
    start_time = time.time()
    try:
        while time.time() - start_time < duration and not stop_recording.is_set():
            time.sleep(0.1)
            # Check if any process died
            for name, proc in list(processes.items()):
                if proc.poll() is not None:
                    print(f"  Warning: {name} recording stopped unexpectedly")
                    del processes[name]
            if not processes:
                break
    except KeyboardInterrupt:
        pass

    # Stop all recordings
    for name, proc in processes.items():
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"  {name} done.")

    elapsed = time.time() - start_time
    print(f"\nRecorded {elapsed:.1f} seconds")

    return output_files


def main():
    parser = argparse.ArgumentParser(
        description="Record from Korg NTS-1 and Android mic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                     # Record both sources for 30s
  %(prog)s -d 60               # Record for 60 seconds
  %(prog)s --synth-only        # Record only the synth
  %(prog)s --mic-only          # Record only the Android mic
  %(prog)s --mix-only          # Record mixed output only
  %(prog)s --list              # List available sources
        """,
    )
    parser.add_argument("--list", action="store_true", help="List available audio devices")
    parser.add_argument("--duration", "-d", type=float, default=30, help="Recording duration in seconds")
    parser.add_argument("--output", "-o", type=Path, default=Path("recordings"), help="Output directory")
    parser.add_argument("--synth-only", action="store_true", help="Record only from synth")
    parser.add_argument("--mic-only", action="store_true", help="Record only from mic (scrcpy)")
    parser.add_argument("--mix-only", action="store_true", help="Record only the mixed output")
    parser.add_argument("--no-scrcpy", action="store_true", help="Don't start scrcpy (use existing)")
    parser.add_argument("--no-monitor", action="store_true", help="Don't connect to output (silent recording)")

    args = parser.parse_args()

    setup = AudioSetup()

    if args.list:
        print("=== Audio Sources ===\n")
        for s in setup.list_sources():
            print(f"  {s['name']}")
            print(f"    {s.get('description', 'N/A')}")
            print()
        print("=== Audio Sinks ===\n")
        for s in setup.list_sinks():
            print(f"  {s['name']}")
            print(f"    {s.get('description', 'N/A')}")
            print()
        return

    # Set up the recording chain
    print("Setting up audio routing...")
    sources = setup.setup_recording(
        with_scrcpy=not args.no_scrcpy and not args.synth_only,
        connect_to_output=not args.no_monitor,
    )

    # Determine what to record
    targets = {}

    if args.mix_only:
        targets["mix"] = sources.get("mix")
    elif args.synth_only:
        targets["synth"] = sources.get("synth")
    elif args.mic_only:
        # For mic-only, record from scrcpy directly
        targets["mic"] = "scrcpy"
    else:
        # Record everything separately
        if sources.get("synth"):
            targets["synth"] = sources["synth"]
        if sources.get("mic"):
            targets["mic"] = "scrcpy"
        if sources.get("mix"):
            targets["mix"] = sources["mix"]

    if not any(targets.values()):
        print("No sources available to record!")
        setup.cleanup()
        sys.exit(1)

    print(f"\nRecording targets:")
    for name, target in targets.items():
        if target:
            print(f"  {name}: {target}")

    print(f"\nRecording for {args.duration} seconds...")
    print("Press Ctrl+C to stop early and save.\n")

    try:
        output_files = record_with_pw(targets, args.duration, args.output)

        print("\nSaved files:")
        for name, path in output_files.items():
            if path.exists():
                size = path.stat().st_size / 1024
                print(f"  {name}: {path} ({size:.1f} KB)")
    finally:
        # Ignore Ctrl+C during cleanup to allow fade-out to complete
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        setup.cleanup()
        signal.signal(signal.SIGINT, signal.SIG_DFL)


if __name__ == "__main__":
    main()
