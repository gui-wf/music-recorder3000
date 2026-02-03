#!/usr/bin/env python3
"""
Audio setup utility for recording from multiple sources.
Handles PipeWire virtual devices, scrcpy, and audio routing.
"""

import atexit
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Fade settings
FADE_DURATION = 0.5  # seconds
FADE_STEPS = 20


@dataclass
class AudioSource:
    """Represents an audio source that can be recorded."""
    name: str
    node_name: str
    channels: int
    sample_rate: int


class AudioSetup:
    """Manages PipeWire audio routing for multi-source recording."""

    def __init__(self):
        self.virtual_sink_id: int | None = None
        self.monitor_sink_id: int | None = None
        self.scrcpy_process: subprocess.Popen | None = None
        self._cleanup_registered = False
        self._created_links: list[tuple[str, str]] = []  # Track links we created
        self._monitor_links: list[tuple[str, str]] = []  # Links for monitoring (to output)
        self._managed_sources: list[str] = []  # Sources we're managing volume for
        self._virtual_sink_name: str | None = None
        self._monitor_sink_name: str | None = None
        self._default_sink: str | None = None
        self._monitoring_enabled: bool = False

    def _run(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a command and return result."""
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    def _run_pactl(self, *args: str) -> str:
        """Run pactl command."""
        result = self._run(["pactl", *args])
        return result.stdout.strip()

    def _run_pw_cli(self, *args: str) -> str:
        """Run pw-cli command."""
        result = self._run(["pw-cli", *args], check=False)
        return result.stdout.strip()

    def _run_pw_link(self, *args: str) -> bool:
        """Run pw-link command, return success."""
        result = self._run(["pw-link", *args], check=False)
        return result.returncode == 0

    def list_sources(self) -> list[dict]:
        """List available audio sources."""
        result = self._run(["pactl", "-f", "json", "list", "sources"])
        return json.loads(result.stdout) if result.stdout else []

    def list_sinks(self) -> list[dict]:
        """List available audio sinks."""
        result = self._run(["pactl", "-f", "json", "list", "sinks"])
        return json.loads(result.stdout) if result.stdout else []

    def get_default_sink(self) -> str | None:
        """Get the default audio sink name."""
        try:
            return self._run_pactl("get-default-sink")
        except subprocess.CalledProcessError:
            return None

    def find_source_by_name(self, pattern: str) -> dict | None:
        """Find a source matching the pattern."""
        sources = self.list_sources()
        pattern_lower = pattern.lower()
        for source in sources:
            name = source.get("name", "")
            desc = source.get("description", "")
            if pattern_lower in name.lower() or pattern_lower in desc.lower():
                return source
        return None

    def find_usb_audio_source(self) -> dict | None:
        """Find the USB audio interface input (not monitor)."""
        sources = self.list_sources()
        for source in sources:
            name = source.get("name", "")
            # Look for alsa_input with usb - this is the actual capture device
            if "alsa_input" in name and "usb" in name.lower():
                return source
        # Fallback to any USB source that's not a monitor
        for source in sources:
            name = source.get("name", "")
            desc = source.get("description", "")
            if "usb" in name.lower() or "usb" in desc.lower():
                if "monitor" not in name.lower() and "monitor" not in desc.lower():
                    return source
        return None

    def create_virtual_sink(self, name: str = "record_mix") -> tuple[str | None, int | None]:
        """
        Create a virtual sink for mixing/recording.
        Returns (sink_name, module_id) if successful.
        """
        try:
            # Check if it already exists
            sinks = self.list_sinks()
            for sink in sinks:
                if sink.get("name") == name:
                    print(f"Virtual sink '{name}' already exists")
                    return name, None

            # Create the null sink
            result = self._run_pactl(
                "load-module", "module-null-sink",
                f"sink_name={name}",
                f"sink_properties=device.description={name}"
            )
            module_id = int(result) if result.isdigit() else None
            print(f"Created virtual sink: {name}")

            # Register cleanup
            if not self._cleanup_registered:
                atexit.register(self.cleanup)
                self._cleanup_registered = True

            return name, module_id
        except Exception as e:
            print(f"Failed to create virtual sink: {e}")
            return None, None

    def remove_virtual_sink(self):
        """Remove the virtual sinks."""
        if self.monitor_sink_id is not None:
            try:
                self._run_pactl("unload-module", str(self.monitor_sink_id))
                print("Removed monitor sink")
            except Exception:
                pass
            self.monitor_sink_id = None

        if self.virtual_sink_id is not None:
            try:
                self._run_pactl("unload-module", str(self.virtual_sink_id))
                print("Removed virtual sink")
            except Exception:
                pass
            self.virtual_sink_id = None

    def get_node_ports(self, node_name: str) -> dict[str, list[str]]:
        """Get input and output ports for a node."""
        result = self._run(["pw-link", "-o"], check=False)  # output ports
        outputs = [l.strip() for l in result.stdout.splitlines() if node_name in l]

        result = self._run(["pw-link", "-i"], check=False)  # input ports
        inputs = [l.strip() for l in result.stdout.splitlines() if node_name in l]

        return {"outputs": outputs, "inputs": inputs}

    def link_ports(self, output_port: str, input_port: str, is_monitor_link: bool = False) -> bool:
        """Link an output port to an input port."""
        if self._run_pw_link(output_port, input_port):
            self._created_links.append((output_port, input_port))
            if is_monitor_link:
                self._monitor_links.append((output_port, input_port))
            return True
        return False

    def unlink_ports(self, output_port: str, input_port: str) -> bool:
        """Unlink an output port from an input port."""
        result = self._run(["pw-link", "-d", output_port, input_port], check=False)
        return result.returncode == 0

    def remove_all_links(self):
        """Remove all links we created."""
        for out_port, in_port in self._created_links:
            if self.unlink_ports(out_port, in_port):
                print(f"Unlinked: {out_port} -> {in_port}")
        self._created_links.clear()

    def disconnect_from_default_sink(self, source_pattern: str) -> int:
        """Disconnect any existing links from a source to the default sink."""
        default_sink = self.get_default_sink()
        if not default_sink:
            return 0

        # Get all existing links
        # Format is:
        #   source:port
        #     |-> dest:port
        result = self._run(["pw-link", "-l"], check=False)
        count = 0
        current_output = None

        for line in result.stdout.splitlines():
            if not line.startswith(" ") and ":" in line:
                # This is an output port line
                current_output = line.strip()
            elif "|-> " in line and current_output:
                # This is a destination for current_output
                dest = line.split("|-> ")[1].strip() if "|-> " in line else None
                if dest and source_pattern.lower() in current_output.lower() and default_sink.lower() in dest.lower():
                    if self.unlink_ports(current_output, dest):
                        print(f"  Disconnected auto-link: {current_output} -> {dest}")
                        count += 1

        return count

    def set_source_volume(self, source_name: str, volume_percent: int) -> bool:
        """Set volume on a source (0-100)."""
        try:
            self._run(["pactl", "set-source-volume", source_name, f"{volume_percent}%"])
            return True
        except Exception:
            return False

    def set_sink_volume(self, sink_name: str, volume_percent: int) -> bool:
        """Set volume on a sink (0-100)."""
        try:
            self._run(["pactl", "set-sink-volume", sink_name, f"{volume_percent}%"])
            return True
        except Exception:
            return False

    def fade_in(self, duration: float = FADE_DURATION):
        """Fade in all managed audio sources."""
        print("  [Fading in...]", end="", flush=True)
        step_delay = duration / FADE_STEPS

        for step in range(FADE_STEPS + 1):
            volume = int((step / FADE_STEPS) * 100)

            for source in self._managed_sources:
                self.set_source_volume(source, volume)

            if self._virtual_sink_name:
                self.set_sink_volume(self._virtual_sink_name, volume)

            time.sleep(step_delay)
        print(" done")

    def fade_out(self, duration: float = FADE_DURATION):
        """Fade out all managed audio sources."""
        print("  [Fading out...]", end="", flush=True)
        step_delay = duration / FADE_STEPS

        for step in range(FADE_STEPS, -1, -1):
            volume = int((step / FADE_STEPS) * 100)

            for source in self._managed_sources:
                self.set_source_volume(source, volume)

            if self._virtual_sink_name:
                self.set_sink_volume(self._virtual_sink_name, volume)

            time.sleep(step_delay)
        print(" done")

    def _fade_sink_only(self, sink_name: str, start: int, end: int, duration: float = FADE_DURATION):
        """Fade only a specific sink's volume (not sources)."""
        steps = max(1, int(duration / 0.025))  # ~25ms per step
        step_delay = duration / steps

        for step in range(steps + 1):
            progress = step / steps
            volume = int(start + (end - start) * progress)
            self.set_sink_volume(sink_name, volume)
            time.sleep(step_delay)

    def enable_monitoring(self):
        """Enable monitoring by reconnecting monitor_mix to output."""
        if self._monitoring_enabled:
            return

        if not self._monitor_sink_name or not self._default_sink:
            print("No monitor sink configured")
            return

        # Reconnect monitor_mix to headphones
        print("  [Enabling monitor...]", end="", flush=True)
        self.set_sink_volume(self._monitor_sink_name, 0)
        self.connect_source_to_sink(self._monitor_sink_name, self._default_sink)
        self._fade_sink_only(self._monitor_sink_name, 0, 100, FADE_DURATION)
        self._monitoring_enabled = True
        print(" done")

    def disable_monitoring(self):
        """Disable monitoring by disconnecting monitor_mix from output."""
        if not self._monitoring_enabled:
            return

        if not self._monitor_sink_name:
            return

        print("  [Disabling monitor...]", end="", flush=True)
        # Fade out first
        self._fade_sink_only(self._monitor_sink_name, 100, 0, FADE_DURATION)

        # Disconnect monitor_mix from headphones
        for out_port, in_port in list(self._created_links):
            if self._monitor_sink_name.lower() in out_port.lower() and self._default_sink and self._default_sink.lower() in in_port.lower():
                self.unlink_ports(out_port, in_port)
                self._created_links.remove((out_port, in_port))

        self._monitoring_enabled = False
        print(" done")

    def toggle_monitoring(self) -> bool:
        """Toggle monitoring on/off. Returns new state."""
        if self._monitoring_enabled:
            self.disable_monitoring()
        else:
            self.enable_monitoring()
        return self._monitoring_enabled

    def _connect_source_to_output(self, source_pattern: str, sink_pattern: str) -> bool:
        """Connect a source to output sink, tracking as monitor link."""
        result = self._run(["pw-link", "-o"], check=False)
        source_ports = [l.strip() for l in result.stdout.splitlines()
                        if source_pattern.lower() in l.lower()]

        result = self._run(["pw-link", "-i"], check=False)
        sink_ports = [l.strip() for l in result.stdout.splitlines()
                      if sink_pattern.lower() in l.lower()]

        if not source_ports or not sink_ports:
            return False

        success = False
        for src_port in source_ports:
            for dst_port in sink_ports:
                src_ch = src_port.split(":")[-1].upper() if ":" in src_port else ""
                dst_ch = dst_port.split(":")[-1].upper() if ":" in dst_port else ""

                # Check for mono (must end with _MONO, not just contain MONO like MONITOR)
                src_is_mono = src_ch.endswith("_MONO") or src_ch == "MONO"
                dst_is_mono = dst_ch.endswith("_MONO") or dst_ch == "MONO"

                # Check for stereo channels
                src_is_left = src_ch.endswith("_FL") or src_ch.endswith("_L")
                src_is_right = src_ch.endswith("_FR") or src_ch.endswith("_R")
                dst_is_left = dst_ch.endswith("_FL") or dst_ch.endswith("_L")
                dst_is_right = dst_ch.endswith("_FR") or dst_ch.endswith("_R")

                should_connect = (
                    src_is_mono or
                    dst_is_mono or
                    (src_is_left and dst_is_left) or
                    (src_is_right and dst_is_right)
                )

                if should_connect:
                    if self.link_ports(src_port, dst_port, is_monitor_link=True):
                        success = True

        return success

    def connect_to_virtual_sink(self, source_node: str, virtual_sink: str = "record_mix") -> bool:
        """Connect a source node's output to the virtual sink."""
        # Get output ports from source
        source_ports = self.get_node_ports(source_node)

        # Get input ports from virtual sink
        sink_ports = self.get_node_ports(virtual_sink)

        if not source_ports["outputs"]:
            print(f"No output ports found for {source_node}")
            return False

        if not sink_ports["inputs"]:
            print(f"No input ports found for {virtual_sink}")
            return False

        # Link FL to FL, FR to FR (or mono to both)
        success = True
        for out_port in source_ports["outputs"]:
            for in_port in sink_ports["inputs"]:
                # Match channels: FL->FL, FR->FR, MONO->both
                out_ch = out_port.split(":")[-1].upper() if ":" in out_port else ""
                in_ch = in_port.split(":")[-1].upper() if ":" in in_port else ""

                # Check for mono (must end with _MONO, not just contain MONO like MONITOR)
                out_is_mono = out_ch.endswith("_MONO") or out_ch == "MONO"

                # Check for stereo channels
                out_is_left = out_ch.endswith("_FL") or out_ch.endswith("_L")
                out_is_right = out_ch.endswith("_FR") or out_ch.endswith("_R")
                in_is_left = in_ch.endswith("_FL") or in_ch.endswith("_L")
                in_is_right = in_ch.endswith("_FR") or in_ch.endswith("_R")

                should_connect = (
                    out_is_mono or
                    (out_is_left and in_is_left) or
                    (out_is_right and in_is_right)
                )

                if should_connect:
                    if self.link_ports(out_port, in_port):
                        print(f"Linked: {out_port} -> {in_port}")
                    else:
                        success = False

        return success

    def connect_virtual_sink_to_output(self, virtual_sink: str = "record_mix", target_sink: str | None = None) -> bool:
        """Connect virtual sink monitor to the default/specified output so you can hear it."""
        if target_sink is None:
            target_sink = self.get_default_sink()

        if not target_sink:
            print("No target sink found")
            return False

        # The virtual sink's monitor outputs need to connect to the target sink's inputs
        monitor_name = f"{virtual_sink}.monitor"
        source_ports = self.get_node_ports(monitor_name)
        sink_ports = self.get_node_ports(target_sink)

        if not source_ports["outputs"]:
            # Try alternate naming
            source_ports = self.get_node_ports(virtual_sink)

        success = True
        for out_port in source_ports.get("outputs", []):
            for in_port in sink_ports.get("inputs", []):
                out_ch = out_port.split("_")[-1] if "_" in out_port else ""
                in_ch = in_port.split("_")[-1] if "_" in in_port else ""
                if out_ch == in_ch:
                    if self.link_ports(out_port, in_port):
                        print(f"Monitor linked: {out_port} -> {in_port}")

        return success

    def start_scrcpy(self, audio_only: bool = True) -> subprocess.Popen | None:
        """
        Start scrcpy to capture Android mic audio.
        Returns the process handle.
        """
        if self.scrcpy_process is not None and self.scrcpy_process.poll() is None:
            print("scrcpy already running")
            return self.scrcpy_process

        cmd = ["scrcpy"]
        if audio_only:
            cmd.extend(["--no-video"])
        cmd.extend([
            "--audio-source=mic",
            "--audio-codec=raw",
        ])

        try:
            self.scrcpy_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            print("Started scrcpy")

            # Register cleanup
            if not self._cleanup_registered:
                atexit.register(self.cleanup)
                self._cleanup_registered = True

            # Wait a moment for scrcpy to initialize
            time.sleep(2)

            return self.scrcpy_process
        except FileNotFoundError:
            print("scrcpy not found - make sure you're in the nix develop shell")
            return None
        except Exception as e:
            print(f"Failed to start scrcpy: {e}")
            return None

    def stop_scrcpy(self):
        """Stop the scrcpy process."""
        if self.scrcpy_process is not None:
            self.scrcpy_process.terminate()
            try:
                self.scrcpy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.scrcpy_process.kill()
            self.scrcpy_process = None
            print("Stopped scrcpy")

    def is_scrcpy_running(self) -> bool:
        """Check if scrcpy is running."""
        if self.scrcpy_process is None:
            return False
        return self.scrcpy_process.poll() is None

    def wait_for_node(self, node_pattern: str, timeout: float = 10.0) -> bool:
        """Wait for a PipeWire node to appear."""
        start = time.time()
        while time.time() - start < timeout:
            result = self._run(["pw-link", "-o"], check=False)
            if node_pattern.lower() in result.stdout.lower():
                return True
            time.sleep(0.5)
        return False

    def connect_source_to_sink(self, source_pattern: str, sink_pattern: str, debug: bool = False) -> bool:
        """
        Connect an audio source (capture device) to a sink.
        This routes capture_* ports to playback_* ports.
        """
        # Get all output ports (sources have capture_* as outputs in pw-link)
        result = self._run(["pw-link", "-o"], check=False)
        source_ports = [l.strip() for l in result.stdout.splitlines()
                        if source_pattern.lower() in l.lower()]

        # Get all input ports for the sink
        result = self._run(["pw-link", "-i"], check=False)
        sink_ports = [l.strip() for l in result.stdout.splitlines()
                      if sink_pattern.lower() in l.lower()]

        if debug:
            print(f"  DEBUG: source_pattern='{source_pattern}' -> ports: {source_ports}")
            print(f"  DEBUG: sink_pattern='{sink_pattern}' -> ports: {sink_ports}")

        if not source_ports:
            print(f"No output ports found matching '{source_pattern}'")
            return False

        if not sink_ports:
            print(f"No input ports found matching '{sink_pattern}'")
            return False

        # Connect mono to both L and R, or match channels
        success = False
        for src_port in source_ports:
            for dst_port in sink_ports:
                # Check if this is a sensible connection
                # Port names are like "node:capture_MONO" or "node:monitor_FL"
                src_ch = src_port.split(":")[-1].upper() if ":" in src_port else ""
                dst_ch = dst_port.split(":")[-1].upper() if ":" in dst_port else ""

                # Check for mono (must end with _MONO, not just contain MONO like MONITOR)
                src_is_mono = src_ch.endswith("_MONO") or src_ch == "MONO"
                dst_is_mono = dst_ch.endswith("_MONO") or dst_ch == "MONO"

                # Check for stereo channels
                src_is_left = src_ch.endswith("_FL") or src_ch.endswith("_L")
                src_is_right = src_ch.endswith("_FR") or src_ch.endswith("_R")
                dst_is_left = dst_ch.endswith("_FL") or dst_ch.endswith("_L")
                dst_is_right = dst_ch.endswith("_FR") or dst_ch.endswith("_R")

                # Mono connects to everything, or match L/R channels
                should_connect = (
                    src_is_mono or
                    dst_is_mono or
                    (src_is_left and dst_is_left) or
                    (src_is_right and dst_is_right)
                )

                if should_connect:
                    if self.link_ports(src_port, dst_port):
                        print(f"Connected: {src_port} -> {dst_port}")
                        success = True

        return success

    def setup_recording(
        self,
        with_scrcpy: bool = True,
        with_synth: bool = True,
        connect_to_output: bool = True,
    ) -> dict[str, str | None]:
        """
        Set up the full recording chain:
        1. Create virtual sink for mixing (record_mix)
        2. Create monitor sink for independent volume control (monitor_mix)
        3. Optionally start scrcpy
        4. Connect USB audio capture and/or scrcpy to record_mix
        5. Route record_mix -> monitor_mix -> headphones for monitoring

        Architecture:
            USB Audio ──┐
                        ↓
                   record_mix ──────→ pw-record (unaffected by monitor toggle)
                        │
            scrcpy ─────┘
                        ↓
                   monitor_mix ──→ headphones (faded independently)

        Returns dict with source names for recording.
        """
        sources = {}

        # Create virtual sink for recording
        virtual_sink, self.virtual_sink_id = self.create_virtual_sink("record_mix")
        if not virtual_sink:
            print("Failed to create virtual sink")
            return sources

        self._virtual_sink_name = virtual_sink

        # Wait for it to appear
        time.sleep(0.5)

        # Set initial volume to 0 for smooth fade-in
        self.set_sink_volume(virtual_sink, 0)

        # Get default output sink for monitoring
        default_sink = self.get_default_sink()
        self._default_sink = default_sink

        # Create monitor sink for independent monitoring control
        if connect_to_output:
            monitor_sink, self.monitor_sink_id = self.create_virtual_sink("monitor_mix")
            if monitor_sink:
                self._monitor_sink_name = monitor_sink
                # Wait for monitor_mix ports to appear in PipeWire
                if not self.wait_for_node("monitor_mix", timeout=5.0):
                    print("Warning: monitor_mix ports did not appear in time")
                self.set_sink_volume(monitor_sink, 0)  # Start muted for fade-in

        # Find and connect USB audio (synth)
        if with_synth:
            usb_source = self.find_usb_audio_source()
            if usb_source:
                sources["synth"] = usb_source["name"]
                synth_name = usb_source["name"]  # e.g., alsa_input.usb-...-00.mono-fallback
                synth_desc = usb_source.get('description', synth_name)
                print(f"Found USB audio: {synth_desc} ({synth_name})")

                # Track this source for volume management
                self._managed_sources.append(synth_name)
                self.set_source_volume(synth_name, 0)  # Start at 0 for fade-in

                # Connect USB audio capture to virtual sink for mixing
                self.connect_source_to_sink(synth_name, virtual_sink)
            else:
                print("USB audio interface not found")
                sources["synth"] = None
        else:
            sources["synth"] = None

        # Start scrcpy if requested
        if with_scrcpy:
            proc = self.start_scrcpy()
            if proc:
                # Wait for scrcpy node to appear
                if self.wait_for_node("scrcpy"):
                    sources["mic"] = "scrcpy"
                    # Disconnect scrcpy's auto-created link to headphones
                    # (scrcpy auto-connects to default output, bypassing our routing)
                    time.sleep(0.3)  # Give PipeWire time to create the auto-link
                    self.disconnect_from_default_sink("scrcpy")
                    # Connect scrcpy to virtual sink
                    self.connect_to_virtual_sink("scrcpy", virtual_sink)
                else:
                    print("scrcpy node did not appear")
                    sources["mic"] = None
            else:
                sources["mic"] = None
        else:
            sources["mic"] = None

        # The virtual sink's monitor is what we record the mix from
        sources["mix"] = f"{virtual_sink}.monitor"

        # Set up monitoring chain: record_mix monitor -> monitor_mix -> headphones
        if connect_to_output and self._monitor_sink_name and default_sink:
            print(f"  Setting up monitoring: {virtual_sink} -> {self._monitor_sink_name} -> {default_sink}")

            # Disconnect any direct links from record_mix to headphones
            # (PipeWire may auto-create these, bypassing our monitor_mix)
            self.disconnect_from_default_sink(virtual_sink)

            # Connect record_mix monitor to monitor_mix
            self.connect_source_to_sink(f"{virtual_sink}", self._monitor_sink_name, debug=True)

            # Connect monitor_mix to headphones
            self.connect_source_to_sink(self._monitor_sink_name, default_sink, debug=True)

            self._monitoring_enabled = True

        # Fade in record_mix for smooth start
        self.fade_in()

        # Also fade in monitor_mix if enabled
        if self._monitoring_enabled and self._monitor_sink_name:
            self._fade_sink_only(self._monitor_sink_name, 0, 100, FADE_DURATION)

        return sources

    def cleanup(self):
        """Clean up all resources with smooth fade-out."""
        # Fade out monitor sink first (if enabled)
        if self._monitoring_enabled and self._monitor_sink_name:
            self._fade_sink_only(self._monitor_sink_name, 100, 0, FADE_DURATION)

        # Fade out record_mix and sources
        if self._managed_sources or self._virtual_sink_name:
            self.fade_out()

        self.remove_all_links()
        self.stop_scrcpy()
        self.remove_virtual_sink()

        # Clear managed state
        self._managed_sources.clear()
        self._virtual_sink_name = None
        self._monitor_sink_name = None
        self._monitoring_enabled = False

    def get_monitor_source(self, sink_name: str = "record_mix") -> str:
        """Get the monitor source name for a sink."""
        return f"{sink_name}.monitor"


def record_with_pw_record(
    target: str,
    output_path: Path,
    duration: float | None = None,
) -> subprocess.Popen:
    """
    Start pw-record to capture from a target.
    Returns the process (call .wait() or .terminate() to stop).
    """
    cmd = ["pw-record", "--target", target]
    if duration:
        # pw-record doesn't have duration, we'll handle it externally
        pass
    cmd.append(str(output_path))

    return subprocess.Popen(cmd)


# Convenience functions for use as a module

_setup: AudioSetup | None = None


def get_setup() -> AudioSetup:
    """Get or create the global AudioSetup instance."""
    global _setup
    if _setup is None:
        _setup = AudioSetup()
    return _setup


def setup_all(with_scrcpy: bool = True, connect_to_output: bool = True) -> dict[str, str | None]:
    """Convenience function to set up everything."""
    return get_setup().setup_recording(with_scrcpy, connect_to_output)


def cleanup_all():
    """Convenience function to clean up everything."""
    global _setup
    if _setup is not None:
        _setup.cleanup()
        _setup = None


if __name__ == "__main__":
    # Test the setup
    import sys

    setup = AudioSetup()

    if "--list" in sys.argv:
        print("=== Sources ===")
        for s in setup.list_sources():
            print(f"  {s['name']}: {s.get('description', 'N/A')}")
        print("\n=== Sinks ===")
        for s in setup.list_sinks():
            print(f"  {s['name']}: {s.get('description', 'N/A')}")
        sys.exit(0)

    print("Setting up recording chain...")
    sources = setup.setup_recording(with_scrcpy=True, connect_to_output=True)
    print(f"\nSources ready: {sources}")

    print("\nPress Enter to clean up...")
    input()
    setup.cleanup()
