"""Edwards nAPG200 / APG200 digital Pirani — link + protocol."""

from __future__ import annotations

import math
import socket
import threading
import time
from typing import Callable, Optional

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
    if transport == "simulated":
        return SimulatedLink()
    raise ValueError(
        f"transport must be 'serial', 'wifi', or 'simulated', got {transport!r}"
    )


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


# A repeating chamber: rest at atmosphere, pump to ~1 Pa, hold, vent.
# The drop is several decades in a few seconds, so the default auto-trigger
# (0.30 decades in 2 s) fires, and the vent climbs back through 95%.
SIM_ATMOSPHERE_PA = 101_325.0
SIM_BASE_PA = 1.0
SIM_HOLD_S = 8.0
SIM_PUMP_S = 4.0
SIM_BASE_HOLD_S = 6.0
SIM_VENT_S = 3.0
SIM_CYCLE_S = SIM_HOLD_S + SIM_PUMP_S + SIM_BASE_HOLD_S + SIM_VENT_S
SIM_SERIAL = "SIMULATED"

# Selected-unit code → pascals per unit, so 101325 Pa is 1013.25 mbar and 760 Torr.
_PA_PER_UNIT = {1: 100.0, 2: 1.0, 3: 101_325.0 / 760.0}


def _log_lerp(start: float, end: float, frac: float) -> float:
    frac = min(1.0, max(0.0, frac))
    return 10 ** (math.log10(start) + frac * (math.log10(end) - math.log10(start)))


def simulated_pressure_pa(elapsed_s: float) -> float:
    """Chamber pressure (Pa) at ``elapsed_s`` into the repeating pump-down."""
    t = elapsed_s % SIM_CYCLE_S
    if t < SIM_HOLD_S:
        return SIM_ATMOSPHERE_PA * (1.0 + 0.01 * math.sin(2.0 * t))
    t -= SIM_HOLD_S
    if t < SIM_PUMP_S:
        return _log_lerp(SIM_ATMOSPHERE_PA, SIM_BASE_PA, t / SIM_PUMP_S)
    t -= SIM_PUMP_S
    if t < SIM_BASE_HOLD_S:
        return SIM_BASE_PA * (1.0 + 0.02 * math.sin(3.0 * t))
    t -= SIM_BASE_HOLD_S
    return _log_lerp(SIM_BASE_PA, SIM_ATMOSPHERE_PA, t / SIM_VENT_S)


class SimulatedLink:
    """In-process gauge that answers the Edwards ASCII protocol.

    Pressure idles near atmosphere, falls to about 1 Pa, holds, then vents,
    and repeats. No serial port or WiFi bridge is opened.
    """

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.time
        self._t0 = self._clock()
        self._units = 2  # Pa, until the app sends !S755
        self._pending = b""
        self._lock = threading.Lock()
        self.closed = False

    def write(self, data: bytes) -> None:
        msg = data.decode("ascii", "replace").strip("\r\n")
        if ":" in msg[:6]:
            msg = msg.split(":", 1)[1]
        reply = self._reply_for(msg)
        with self._lock:
            self._pending = reply

    def read_line(self) -> bytes:
        with self._lock:
            reply, self._pending = self._pending, b""
        return reply

    def set_timeout(self, seconds: float) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def _reply_for(self, msg: str) -> bytes:
        head, _, arg = msg.partition(" ")
        if head == "?S751":
            return b"=S751 nAPG200 (simulated);SIM;0000\r"
        if head == "?S790":
            return f"=S790 {SIM_SERIAL}\r".encode("ascii")
        if head == "!S755":
            try:
                code = int(arg)
            except ValueError:
                code = 0
            if code not in _PA_PER_UNIT:
                return b"*S755 04\r"
            with self._lock:
                self._units = code
            return b"*S755 00\r"
        if head == "?V752":
            with self._lock:
                units = self._units
            elapsed = self._clock() - self._t0
            pressure = simulated_pressure_pa(elapsed) / _PA_PER_UNIT[units]
            status = f"{(units & 0x3) << 4:04X}"
            return f"=V752 {pressure:.2E};{status}\r".encode("ascii")
        return b"*S000 02\r"


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
