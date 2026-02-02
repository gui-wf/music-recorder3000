#!/usr/bin/env python3
"""
Monitor audio from input devices through default output.
Sets up PipeWire routing so you can hear all sources mixed together.
"""

import argparse
import signal
import sys
import time

from audio_setup import AudioSetup


def main():
    parser = argparse.ArgumentParser(
        description="Monitor audio inputs through default output",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                  # Monitor both synth and Android mic
  %(prog)s --synth-only     # Monitor only the synth
  %(prog)s --mic-only       # Monitor only the Android mic
  %(prog)s --list           # List available devices
        """,
    )
    parser.add_argument("--list", action="store_true", help="List available audio devices")
    parser.add_argument("--synth-only", action="store_true", help="Monitor only synth")
    parser.add_argument("--mic-only", action="store_true", help="Monitor only mic (scrcpy)")
    parser.add_argument("--no-scrcpy", action="store_true", help="Don't start scrcpy (use existing)")

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

    # Track if we're stopping
    stopping = False

    def signal_handler(sig, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            print("\nStopping...")

    signal.signal(signal.SIGINT, signal_handler)

    # Determine what to set up
    with_scrcpy = not args.no_scrcpy and not args.synth_only

    print("Setting up audio monitoring...")

    if args.synth_only:
        # Just find and report the USB audio - it should already be routable
        usb = setup.find_usb_audio_source()
        if usb:
            print(f"\nSynth source: {usb['description']}")
            print("The synth audio should play through your default output.")
            print("If not, use helvum or qpwgraph to route it.")
        else:
            print("USB audio interface not found!")
            sys.exit(1)

    elif args.mic_only:
        # Start scrcpy - it automatically plays to default output
        if not args.no_scrcpy:
            proc = setup.start_scrcpy()
            if not proc:
                print("Failed to start scrcpy!")
                sys.exit(1)
            print("\nscrcpy started - Android mic audio playing to default output")
        else:
            print("Using existing scrcpy instance")

    else:
        # Set up full monitoring with virtual sink
        sources = setup.setup_recording(
            with_scrcpy=with_scrcpy,
            connect_to_output=True,  # This routes to speakers
        )

        print("\nMonitoring setup complete:")
        if sources.get("synth"):
            print(f"  Synth: {sources['synth']}")
        if sources.get("mic"):
            print(f"  Mic: {sources['mic']} (via scrcpy)")
        if sources.get("mix"):
            print(f"  Mix output: {sources['mix']}")

        print("\nAudio is routed to your default output.")
        print("Use helvum to see/adjust the routing if needed.")

    print("\nPress Ctrl+C to stop...")

    try:
        while not stopping:
            time.sleep(0.2)
            # Check if scrcpy died
            if with_scrcpy and not setup.is_scrcpy_running():
                print("\nscrcpy stopped unexpectedly")
                break
    except KeyboardInterrupt:
        pass
    finally:
        # Ignore Ctrl+C during cleanup to allow fade-out to complete
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        setup.cleanup()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        print("Cleaned up.")


if __name__ == "__main__":
    main()
