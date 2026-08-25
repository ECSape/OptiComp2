# -*- coding: utf-8 -*-
"""Codec + bus-logic tests with a fake serial port (no hardware)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hw import elliptec as ell


class FakeSerial(object):
    """Scripted serial port: maps TX bytes -> list of RX lines (consumed in order)."""

    def __init__(self, port, baudrate, timeout=None):
        self.port = port
        self.timeout = timeout
        self.timeouts_seen = []
        self.script = {}
        self.sent = []
        self.closed = False

    def reset_input_buffer(self):
        pass

    def write(self, tx):
        self.sent.append(tx)
        self._last = tx
        return len(tx)

    def readline(self):
        # Replies for a command are consumed in order; the last one repeats.
        self.timeouts_seen.append(self.timeout)
        lines = self.script.get(self._last)
        if not lines:
            return b""
        if len(lines) > 1:
            return lines.pop(0)
        return lines[0]

    def close(self):
        self.closed = True


def make_bus(script):
    holder = {}

    def factory(port, baudrate, timeout=None):
        s = FakeSerial(port, baudrate, timeout)
        s.script = script
        holder["ser"] = s
        return s

    bus = ell.ElliptecBus("COMX", serial_factory=factory)
    return bus, holder["ser"]


IN_REPLY = b"0IN" + b"0E" + b"12345678" + b"2021" + b"03" + b"01" + b"0168" + b"00023000" + b"\r\n"   # ELL14, SN 12345678, travel 360, pulses 143360


class CodecTests(unittest.TestCase):
    def test_hex32_roundtrip(self):
        for v in (0, 1, 143359, -1, -143360, 2 ** 31 - 1):
            self.assertEqual(ell.parse_hex32(ell.hex32(v)), v)
        self.assertEqual(ell.hex32(-1), "FFFFFFFF")
        self.assertEqual(ell.hex32(0x11FC7), "00011FC7")

    def test_info_decode_full_serial(self):
        info = ell.DeviceInfo.from_reply(IN_REPLY.decode().strip())
        self.assertEqual(info.model, 0x0E)
        self.assertEqual(info.serial, "12345678")        # 8 chars, not 7
        self.assertEqual(info.travel, 360)
        self.assertEqual(info.pulses, 143360)
        self.assertAlmostEqual(info.pulses_per_unit, 143360 / 360.0)

    def test_gs_po_decode(self):
        self.assertEqual(ell.decode_reply("2GS09")["status"], "busy")
        self.assertEqual(ell.decode_reply("3PO00011FC7")["value"], 0x11FC7)
        self.assertEqual(ell.decode_reply("3POFFFFFFFF")["value"], -1)


class BusTests(unittest.TestCase):
    def test_query_timeout_raises(self):
        bus, ser = make_bus({})
        with self.assertRaises(ell.ReplyTimeout):
            bus.info("0")

    def test_info_and_position(self):
        bus, ser = make_bus({b"0in": [IN_REPLY], b"0gp": [b"0PO00023000\r\n"]})
        self.assertEqual(bus.info("0").serial, "12345678")
        self.assertEqual(bus.position("0"), 143360)

    def test_move_waits_while_busy_then_reads_position(self):
        bus, ser = make_bus({
            b"3ma00011FC7": [b"3GS09\r\n"],
            b"3gs": [b"3GS09\r\n", b"3GS00\r\n"],
            b"3gp": [b"3PO00000000\r\n", b"3PO00011FC7\r\n"],
        })
        self.assertEqual(bus.move_abs("3", 0x11FC7), 0x11FC7)
        self.assertIn(b"3gs", ser.sent)

    def test_swallowed_command_is_resent(self):
        # real-bus case 2026-08-25: no reply, no motion, GS00 -> resend; second attempt works
        bus, ser = make_bus({
            b"3ma0000FA72": [b"", b"3PO0000FA72\r\n"],
            b"3gs": [b"3GS00\r\n"],
            b"3gp": [b"3PO0000F438\r\n", b"3PO0000F438\r\n"],
        })
        bus.first_wait = 0.01
        bus.poll_interval = 0
        self.assertEqual(bus.move_abs("3", 0xFA72), 0xFA72)
        self.assertEqual(ser.sent.count(b"3ma0000FA72"), 2)

    def test_swallowed_command_gives_up_after_attempts(self):
        bus, ser = make_bus({b"3ma0000FA72": [b""], b"3gs": [b"3GS00\r\n"], b"3gp": [b"3PO0000F438\r\n"]})
        bus.first_wait = 0.01
        bus.poll_interval = 0
        with self.assertRaises(ell.ElliptecError):
            bus.move_abs("3", 0xFA72)
        self.assertEqual(ser.sent.count(b"3ma0000FA72"), 3)

    def test_wrong_final_position_raises(self):
        bus, ser = make_bus({b"3ma0000FA72": [b"3PO0000F438\r\n"], b"3gp": [b"3PO0000F438\r\n"]})
        with self.assertRaises(ell.ElliptecError):
            bus.move_abs("3", 0xFA72)

    def test_lost_reply_but_move_completed(self):
        bus, ser = make_bus({b"3ma0000FA72": [b""], b"3gs": [b"", b"3GS09\r\n", b"3GS00\r\n"],
                             b"3gp": [b"3PO0000F438\r\n", b"3PO0000FA70\r\n"]})
        bus.first_wait = 0.01
        bus.poll_timeout = 0.01
        bus.poll_interval = 0
        self.assertEqual(bus.move_abs("3", 0xFA72), 0xFA70)          # within tolerance
        self.assertEqual(ser.sent.count(b"3ma0000FA72"), 1)

    def test_motion_uses_long_timeout_and_restores(self):
        bus, ser = make_bus({b"2ma00004600": [b"2PO00004600\r\n"], b"2gs": [b"2GS00\r\n"], b"2gp": [b"2PO00000000\r\n"]})
        bus.move_abs("2", 0x4600)
        self.assertEqual(ser.timeouts_seen[-1], ell.FIRST_WAIT)
        bus.status("2")
        self.assertEqual(ser.timeouts_seen[-1], ell.DEFAULT_TIMEOUT)   # restored after the move

    def test_deferred_po_arrives_during_gs_poll(self):
        # real-bus case: ELL18 silent while moving, completion PO lands in the `gs` read window
        bus, ser = make_bus({b"2ma00004473": [b""], b"2gs": [b"", b"2PO00004473\r\n"], b"2gp": [b"2PO00009C40\r\n"]})
        bus.first_wait = 0.01
        bus.poll_timeout = 0.01
        bus.poll_interval = 0
        self.assertEqual(bus.move_abs("2", 0x4473), 0x4473)
        self.assertEqual(ser.sent.count(b"2ma00004473"), 1)
        self.assertNotIn(b"2gp", ser.sent[1:])                 # position came from the PO, no extra gp

    def test_shutter_no_move_is_not_retried(self):
        bus, ser = make_bus({b"0fw": [b""], b"0gs": [b"0GS00\r\n"], b"0gp": [b"0PO0000001F\r\n"]})
        bus.first_wait = 0.01
        bus.poll_interval = 0
        self.assertEqual(bus.forward("0"), 0x1F)
        self.assertEqual(ser.sent.count(b"0fw"), 1)

    def test_move_error_status_raises(self):
        bus, ser = make_bus({b"2ho0": [b"2GS02\r\n"], b"2gp": [b"2PO00000100\r\n"]})
        with self.assertRaises(ell.DeviceStatusError) as cm:
            bus.home("2")
        self.assertEqual(cm.exception.code, 2)

    def test_wrong_address_reply_raises(self):
        bus, ser = make_bus({b"1gs": [b"2GS00\r\n"]})
        with self.assertRaises(ell.ElliptecError):
            bus.status("1")

    def test_rotation_stage_degrees(self):
        bus, ser = make_bus({b"3in": [IN_REPLY.replace(b"0IN", b"3IN")],
                             b"3ma00011FC7": [b"3PO00011FC7\r\n"],
                             b"3gp": [b"3PO00000000\r\n", b"3PO00011FC7\r\n"]})
        st = ell.RotationStage(bus, "3")
        self.assertEqual(st.deg_to_pulses(185), 0x11FC7)       # same value the original code sent
        self.assertAlmostEqual(st.move_deg(185), 185.0, places=2)
        self.assertAlmostEqual(st.position_deg(), 185.0, places=2)
        self.assertEqual(st.deg_to_pulses(-90), st.deg_to_pulses(270))


if __name__ == "__main__":
    unittest.main()
