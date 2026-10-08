"""Unit tests for gauge protocol (no hardware required)."""

from __future__ import annotations

import unittest

from gauge import (
    NAPG200,
    GaugeError,
    decode_status,
    open_link,
    parse_pressure_payload,
    preferred_serial_port,
    status_flags,
    usable_serial_port,
)


class FakeLink:
    """Queue of CR-terminated replies, one per write."""

    def __init__(self, replies: list[bytes]):
        self.replies = list(replies)
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def read_line(self) -> bytes:
        if not self.replies:
            return b""
        return self.replies.pop(0)

    def close(self) -> None:
        self.closed = True


class TestParse(unittest.TestCase):
    def test_pressure_atmosphere_pa(self):
        p, st = parse_pressure_payload("1.00E+05;0020")
        self.assertAlmostEqual(p, 1e5)
        self.assertEqual(st["units"], "Pa")
        self.assertEqual(st["gas"], "N2/air")
        self.assertEqual(status_flags(st), "ok")

    def test_status_filament_error(self):
        st = decode_status("0401")  # bit0 + bit10
        self.assertTrue(st["gauge_error"])
        self.assertTrue(st["filament_error"])
        self.assertIn("filament_error", status_flags(st))

    def test_open_link_rejects_bad_transport(self):
        with self.assertRaises(ValueError):
            open_link("bluetooth")

    def test_open_link_rejects_empty_serial(self):
        with self.assertRaises(ValueError):
            open_link("serial", serial_port="(no ports)")


class TestNAPG200(unittest.TestCase):
    def test_identity_and_pressure(self):
        link = FakeLink(
            [
                b"=S751 APG200-LC_T_RS485;D02801600E;0000\r",
                b"=S790 260528883\r",
                b"*S755 00\r",
                b"=V752 1.00E+03;0010\r",
            ]
        )
        g = NAPG200(link)
        self.assertEqual(g.identity(), "APG200-LC_T_RS485;D02801600E;0000")
        self.assertEqual(g.serial_number(), "260528883")
        g.set_units("mbar")
        p, st = g.read_pressure()
        self.assertAlmostEqual(p, 1000.0)
        self.assertEqual(st["units"], "mbar")
        self.assertEqual(
            link.writes,
            [b"?S751\r", b"?S790\r", b"!S755 1\r", b"?V752\r"],
        )

    def test_strips_leading_nul(self):
        link = FakeLink([b"\x00=S751 OK\r"])
        g = NAPG200(link)
        self.assertEqual(g.identity(), "OK")

    def test_no_reply(self):
        g = NAPG200(FakeLink([]))
        with self.assertRaises(GaugeError):
            g.identity()

    def test_error_status(self):
        g = NAPG200(FakeLink([b"*S755 04\r"]))
        with self.assertRaises(GaugeError) as ctx:
            g.set_units("mbar")
        self.assertIn("04", str(ctx.exception))

    def test_node_prefix(self):
        link = FakeLink([b"#01:99=S751 HELLO\r"])
        g = NAPG200(link, node=1)
        self.assertEqual(g.identity(), "HELLO")
        self.assertEqual(link.writes[0], b"#01:99?S751\r")

    def test_success_code_single_zero(self):
        # Edwards manuals document '*S755 0' (one digit), not only '00'
        link = FakeLink([b"*S755 0\r", b"=V752 1.00E+03;0010\r"])
        g = NAPG200(link)
        g.set_units("mbar")
        p, st = g.read_pressure()
        self.assertAlmostEqual(p, 1000.0)
        self.assertEqual(st["units"], "mbar")

    def test_success_code_double_zero(self):
        link = FakeLink([b"*S755 00\r"])
        g = NAPG200(link)
        g.set_units("Pa")  # should not raise

    def test_unknown_units(self):
        g = NAPG200(FakeLink([]))
        with self.assertRaises(GaugeError):
            g.set_units("psi")


class TestSerialPortChoice(unittest.TestCase):
    def test_skips_macos_debug_console(self):
        self.assertFalse(usable_serial_port("/dev/cu.debug-console"))
        self.assertFalse(usable_serial_port("/dev/cu.Bluetooth-Incoming-Port"))
        self.assertTrue(usable_serial_port("/dev/cu.usbserial-A50285BI"))

    def test_prefers_usb_adapter(self):
        ports = ["/dev/cu.debug-console", "/dev/cu.usbserial-1420", "/dev/cu.wlan-debug"]
        usable = [p for p in ports if usable_serial_port(p)]
        self.assertEqual(preferred_serial_port(usable), "/dev/cu.usbserial-1420")

    def test_empty_when_only_system_ports(self):
        self.assertEqual(preferred_serial_port([]), "")


if __name__ == "__main__":
    unittest.main()
