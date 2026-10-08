"""Edwards nAPG200 / APG200 digital Pirani — link + protocol."""

from __future__ import annotations

import socket
import time
from typing import Optional

UNITS_MAP = {1: "mbar", 2: "Pa", 3: "Torr"}
UNIT_CODE = {"mbar": 1, "pa": 2, "torr": 3}
GAS_MAP = {0: "N2/air", 1: "Ar", 2: "He", 3: "CO2", 4: "Ne", 5: "Kr", 6: "Xe"}
ERRORS = {
    "01": "invalid command for object ID",
    "02": "invalid query/command for this gauge",
    "03": "missing parameter",
    "04": "parameter out of range",
    "05": "invalid command in current state",
    "06": "data checksum error",
    "07": "EEPROM read/write error",
    "08": "operation timeout (commands sent too fast)",
    "09": "invalid config ID",
}


class GaugeError(RuntimeError):
    pass


class SerialLink:
    def __init__(self, port: str, baud: int = 9600, timeout: float = 2.0):
        import serial
        self.ser = serial.Serial(
            port, baudrate=baud, bytesize=8, parity="N", stopbits=1, timeout=timeout
        )
        time.sleep(0.15)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def read_line(self) -> bytes:
        return self.ser.read_until(b"\r")

    def set_timeout(self, seconds: float) -> None:
        self.ser.timeout = seconds

    def close(self) -> None:
        self.ser.close()


class TcpLink:
    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        # Requests are 6 bytes; without this Nagle can sit on them for ~40 ms.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_line(self) -> bytes:
        while b"\r" not in self.buf:
            try:
                chunk = self.sock.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buf += chunk
        line, sep, rest = self.buf.partition(b"\r")
        if not sep:
            out, self.buf = self.buf, b""
            return out
        self.buf = rest
        return line + sep

    def set_timeout(self, seconds: float) -> None:
        self.sock.settimeout(seconds)

    def close(self) -> None:
        self.sock.close()


def open_link(
    transport: str,
    *,
    serial_port: str = "",
    baud: int = 9600,
    host: str = "10.10.100.254",
    tcp_port: int = 8899,
    timeout: float = 2.0,
):
    if transport == "serial":
        if not serial_port or serial_port.startswith("("):
            raise ValueError("No serial port selected")
        return SerialLink(serial_port, baud, timeout=timeout)
    if transport == "wifi":
        return TcpLink(host, tcp_port, timeout=timeout)
    raise ValueError(f"transport must be 'serial' or 'wifi', got {transport!r}")


# macOS advertises these even when no USB-RS485 adapter is plugged in.
_SKIP_PORT_FRAGMENTS = (
    "bluetooth",
    "debug-console",
    "debugconsole",
    "incoming",
    "outgoing",
)


def usable_serial_port(device: str) -> bool:
    name = device.lower()
    return not any(fragment in name for fragment in _SKIP_PORT_FRAGMENTS)


def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [p.device for p in list_ports.comports() if usable_serial_port(p.device)]


def preferred_serial_port(ports: list[str] | None = None) -> str:
    """USB adapters first; empty if nothing looks like a real gauge link."""
    candidates = ports if ports is not None else list_serial_ports()
    for hint in ("usbserial", "usbmodem", "wchusb", "ftdi", "cp210", "ch34"):
        for port in candidates:
            if hint in port.lower():
                return port
    return ""


def decode_status(status_s: str) -> dict:
    status = int(status_s, 16)
    return {
        "raw_status": status_s,
        "units": UNITS_MAP.get((status >> 4) & 0x3, "?"),
        "gas": GAS_MAP.get((status >> 12) & 0x7, "?"),
        "setpoint_on": bool(status & (1 << 2)),
        "locked": bool(status & (1 << 3)),
        "calibrating": bool(status & (1 << 7)),
        "gauge_error": bool(status & 1),
        "filament_error": bool(status & (1 << 10)),
    }


def parse_pressure_payload(data: str) -> tuple[float, dict]:
    press_s, status_s = data.split(";")
    return float(press_s), decode_status(status_s)


def status_flags(st: dict) -> str:
    keys = ("gauge_error", "filament_error", "calibrating", "locked")
    return ",".join(k for k in keys if st[k]) or "ok"


def status_ok(token: str) -> bool:
    """Edwards replies use '*S755 0' or '*S755 00' for success."""
    digits = "".join(ch for ch in token if ch.isdigit())
    if not digits:
        return False
    try:
        return int(digits) == 0
    except ValueError:
        return False


def status_error_code(token: str) -> str:
    digits = "".join(ch for ch in token if ch.isdigit()) or token
    return digits.zfill(2)[-2:]


class NAPG200:
    def __init__(self, link, node: Optional[int] = None):
        self.link = link
        self.prefix = "" if node is None else f"#{node:02d}:99"

    def _txrx(self, msg: str) -> Optional[str]:
        self.link.write((self.prefix + msg + "\r").encode("ascii"))
        raw = self.link.read_line()
        if not raw:
            raise GaugeError(
                f"no reply to {msg!r} (check power, A/B polarity, baud, GND)"
            )
        reply = raw.decode("ascii", "replace").strip("\r\n").lstrip("\x00")
        if self.prefix and ":" in reply[:6]:
            reply = reply[6:]
        if reply.startswith("*"):
            token = reply.split()[-1]
            if not status_ok(token):
                code = status_error_code(token)
                raise GaugeError(
                    f"{msg} -> error {code}: {ERRORS.get(code, 'unknown')}"
                )
            return None
        if not reply.startswith("="):
            raise GaugeError(f"unexpected reply to {msg!r}: {reply!r}")
        return reply.split(" ", 1)[1] if " " in reply else ""

    def command(self, msg: str) -> Optional[str]:
        out = self._txrx(msg)
        time.sleep(0.05)
        return out

    def identity(self) -> str:
        return self._txrx("?S751") or ""

    def serial_number(self) -> str:
        return self._txrx("?S790") or ""

    def set_units(self, name: str) -> None:
        key = name.lower()
        if key not in UNIT_CODE:
            raise GaugeError(f"unknown units {name!r}")
        self.command(f"!S755 {UNIT_CODE[key]}")

    def read_pressure(self) -> tuple[float, dict]:
        data = self._txrx("?V752")
        if not data:
            raise GaugeError("empty pressure reply")
        return parse_pressure_payload(data)
