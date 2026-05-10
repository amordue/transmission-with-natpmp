#!/usr/bin/env python3

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DEFAULT_SETTINGS_SOURCE = Path("/usr/local/share/transmission/default-settings.json")
SETTINGS_FILENAME = "settings.json"
STARTUP_VPN_TIMEOUT_SECONDS = 30
NATPMP_LEASE_SECONDS = 60
PORT_CHANGE_TIMEOUT_SECONDS = 10
RPC_READY_TIMEOUT_SECONDS = 30
STOP_TIMEOUT_SECONDS = 20
RETRY_DELAY_SECONDS = 5


class RuntimeErrorWithExitCode(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{timestamp}] {message}", flush=True)


def getenv_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeErrorWithExitCode(f"{name} must be an integer, got {raw_value!r}") from exc

    if value < 1:
        raise RuntimeErrorWithExitCode(f"{name} must be greater than zero, got {value}")

    return value


class Supervisor:
    def __init__(self) -> None:
        self.vpn_interface = os.getenv("VPN_INTERFACE", "wg0")
        self.vpn_expected_ipv4 = os.getenv("VPN_EXPECTED_IPV4", "10.2.0.2")
        self.natpmp_gateway = os.getenv("NATPMP_GATEWAY")
        self.transmission_config_dir = Path(os.getenv("TRANSMISSION_CONFIG_DIR", "/config"))
        self.natpmp_renew_interval_seconds = getenv_int("NATPMP_RENEW_INTERVAL_SECONDS", 45)
        self.transmission_process: subprocess.Popen[str] | None = None
        self.stop_requested = False

        if not self.natpmp_gateway:
            raise RuntimeErrorWithExitCode("NATPMP_GATEWAY is required")

    @property
    def settings_path(self) -> Path:
        return self.transmission_config_dir / SETTINGS_FILENAME

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.handle_stop_signal)
        signal.signal(signal.SIGTERM, self.handle_stop_signal)

    def handle_stop_signal(self, signum: int, _frame: object) -> None:
        signal_name = signal.Signals(signum).name
        log(f"Received {signal_name}; shutting down")
        self.stop_requested = True

    def run(self) -> int:
        self.install_signal_handlers()
        self.wait_for_vpn(timeout_seconds=STARTUP_VPN_TIMEOUT_SECONDS)

        settings = self.ensure_settings()
        initial_port = self.wait_for_initial_natpmp_port()
        if settings.get("peer-port") != initial_port:
            settings["peer-port"] = initial_port
            self.write_settings(settings)

        self.start_transmission()
        settings = self.load_settings()
        self.wait_for_rpc(settings)
        self.ensure_runtime_peer_port(initial_port)
        current_port = initial_port

        while not self.stop_requested:
            if not self.vpn_is_healthy():
                raise RuntimeErrorWithExitCode(
                    f"Killswitch triggered because {self.vpn_interface} no longer has {self.vpn_expected_ipv4}"
                )

            if self.transmission_process is None or self.transmission_process.poll() is not None:
                raise RuntimeErrorWithExitCode("Transmission exited unexpectedly")

            try:
                mapped_port = self.request_natpmp_port()
                if mapped_port != current_port:
                    log(f"Mapped public port changed from {current_port} to {mapped_port}")
                    self.update_settings_peer_port(mapped_port)
                    self.reload_or_restart_transmission(mapped_port)
                    current_port = mapped_port
            except RuntimeError as exc:
                log(f"NAT-PMP renewal failed: {exc}; retrying")

            self.sleep_with_checks(self.natpmp_renew_interval_seconds)

        self.stop_transmission()
        return 0

    def wait_for_vpn(self, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.stop_requested:
                raise RuntimeErrorWithExitCode("Shutdown requested before startup completed", exit_code=0)
            if self.vpn_is_healthy():
                log(f"VPN interface {self.vpn_interface} is healthy with {self.vpn_expected_ipv4}")
                return
            time.sleep(1)

        raise RuntimeErrorWithExitCode(
            f"VPN interface {self.vpn_interface} did not reach expected address {self.vpn_expected_ipv4}"
        )

    def vpn_is_healthy(self) -> bool:
        command = ["ip", "-4", "-o", "addr", "show", "dev", self.vpn_interface]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False

        expected_fragment = f"inet {self.vpn_expected_ipv4}/"
        return expected_fragment in result.stdout

    def ensure_settings(self) -> dict:
        self.transmission_config_dir.mkdir(parents=True, exist_ok=True)
        if not self.settings_path.exists():
            if not DEFAULT_SETTINGS_SOURCE.exists():
                raise RuntimeErrorWithExitCode(
                    f"Missing bundled default settings at {DEFAULT_SETTINGS_SOURCE}"
                )
            shutil.copy2(DEFAULT_SETTINGS_SOURCE, self.settings_path)
            log(f"Seeded {self.settings_path} from bundled defaults")

        settings = self.load_settings()
        changed = False

        if settings.get("bind-address-ipv4") != self.vpn_expected_ipv4:
            settings["bind-address-ipv4"] = self.vpn_expected_ipv4
            changed = True

        if settings.get("port-forwarding-enabled") is not False:
            settings["port-forwarding-enabled"] = False
            changed = True

        self.ensure_runtime_directories(settings)

        if changed:
            self.write_settings(settings)

        return settings

    def ensure_runtime_directories(self, settings: dict) -> None:
        directory_keys = ["download-dir"]
        if settings.get("incomplete-dir-enabled"):
            directory_keys.append("incomplete-dir")
        if settings.get("watch-dir-enabled"):
            directory_keys.append("watch-dir")

        for key in directory_keys:
            path_value = settings.get(key)
            if isinstance(path_value, str) and path_value:
                Path(path_value).mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> dict:
        with self.settings_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def write_settings(self, settings: dict) -> None:
        self.transmission_config_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.transmission_config_dir,
            delete=False,
        ) as handle:
            json.dump(settings, handle, indent=4, sort_keys=False)
            handle.write("\n")
            temp_name = handle.name

        os.replace(temp_name, self.settings_path)

    def wait_for_initial_natpmp_port(self) -> int:
        while not self.stop_requested:
            if not self.vpn_is_healthy():
                raise RuntimeErrorWithExitCode(
                    f"Killswitch triggered because {self.vpn_interface} no longer has {self.vpn_expected_ipv4}"
                )

            try:
                port = self.request_natpmp_port()
                log(f"Initial mapped public port is {port}")
                return port
            except RuntimeError as exc:
                log(f"Waiting for initial NAT-PMP mapping: {exc}")
                time.sleep(RETRY_DELAY_SECONDS)

        raise RuntimeErrorWithExitCode("Shutdown requested before NAT-PMP mapping was created", exit_code=0)

    def request_natpmp_port(self) -> int:
        udp_port = self.request_natpmp_mapping("udp")
        tcp_port = self.request_natpmp_mapping("tcp")
        if udp_port != tcp_port:
            log(
                "UDP and TCP NAT-PMP ports differed "
                f"({udp_port} vs {tcp_port}); using the TCP port as requested"
            )
        return tcp_port

    def request_natpmp_mapping(self, protocol: str) -> int:
        command = [
            "natpmpc",
            "-a",
            "1",
            "0",
            protocol,
            str(NATPMP_LEASE_SECONDS),
            "-g",
            self.natpmp_gateway,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            message = stderr or stdout or f"natpmpc exited with {result.returncode}"
            raise RuntimeError(message)

        output = f"{result.stdout}\n{result.stderr}"
        match = re.search(r"Mapped public port\s+(\d+)\s+protocol\s+\w+\s+to local port\s+\d+", output)
        if not match:
            raise RuntimeError(f"Could not parse natpmpc output for {protocol}: {output.strip()}")

        return int(match.group(1))

    def start_transmission(self) -> None:
        if self.transmission_process is not None and self.transmission_process.poll() is None:
            return

        command = [
            "transmission-daemon",
            "--foreground",
            "--config-dir",
            str(self.transmission_config_dir),
            "--log-info",
        ]
        log("Starting Transmission")
        self.transmission_process = subprocess.Popen(command)

    def stop_transmission(self) -> None:
        process = self.transmission_process
        if process is None:
            return

        if process.poll() is not None:
            self.transmission_process = None
            return

        log("Stopping Transmission")
        process.terminate()
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log("Transmission did not stop in time; killing it")
            process.kill()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        finally:
            self.transmission_process = None

    def restart_transmission(self) -> None:
        self.stop_transmission()
        if self.stop_requested:
            return
        self.start_transmission()
        settings = self.load_settings()
        self.wait_for_rpc(settings)

    def wait_for_rpc(self, settings: dict) -> None:
        rpc_port = int(settings.get("rpc-port", 9091))
        rpc_bind_address = settings.get("rpc-bind-address", "0.0.0.0")
        rpc_host = self.resolve_rpc_host(rpc_bind_address)
        deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            if self.transmission_process is None or self.transmission_process.poll() is not None:
                raise RuntimeErrorWithExitCode("Transmission exited before the RPC endpoint became ready")

            if self.tcp_connects(rpc_host, rpc_port):
                log(f"Transmission RPC is listening on {rpc_host}:{rpc_port}")
                return

            time.sleep(1)

        raise RuntimeErrorWithExitCode(
            f"Transmission RPC did not become ready on {rpc_host}:{rpc_port}"
        )

    def resolve_rpc_host(self, bind_address: str) -> str:
        if bind_address in {"0.0.0.0", "::", ""}:
            return "127.0.0.1"
        return bind_address

    def update_settings_peer_port(self, port: int) -> None:
        settings = self.load_settings()
        if settings.get("peer-port") == port:
            return

        settings["peer-port"] = port
        self.write_settings(settings)
        log(f"Updated Transmission peer port in {self.settings_path} to {port}")

    def reload_or_restart_transmission(self, port: int) -> None:
        if self.transmission_process is None or self.transmission_process.poll() is not None:
            raise RuntimeErrorWithExitCode("Transmission is not running when a port update was requested")

        log("Sending SIGHUP to Transmission to reload configuration")
        self.transmission_process.send_signal(signal.SIGHUP)
        if self.wait_for_peer_port(port, PORT_CHANGE_TIMEOUT_SECONDS):
            log(f"Transmission applied peer port {port} after reload")
            return

        log("Transmission did not apply the new peer port after reload; restarting it")
        self.restart_transmission()
        self.ensure_runtime_peer_port(port)

    def ensure_runtime_peer_port(self, port: int) -> None:
        if self.wait_for_peer_port(port, PORT_CHANGE_TIMEOUT_SECONDS):
            return
        raise RuntimeErrorWithExitCode(f"Transmission did not open peer port {port}")

    def wait_for_peer_port(self, port: int, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.transmission_process is None or self.transmission_process.poll() is not None:
                return False

            if self.tcp_connects(self.vpn_expected_ipv4, port):
                return True

            time.sleep(1)

        return False

    def tcp_connects(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def sleep_with_checks(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_requested:
                return
            if not self.vpn_is_healthy():
                raise RuntimeErrorWithExitCode(
                    f"Killswitch triggered because {self.vpn_interface} no longer has {self.vpn_expected_ipv4}"
                )
            if self.transmission_process is None or self.transmission_process.poll() is not None:
                raise RuntimeErrorWithExitCode("Transmission exited unexpectedly")
            time.sleep(1)


def main() -> int:
    try:
        return Supervisor().run()
    except RuntimeErrorWithExitCode as exc:
        log(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        log("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())