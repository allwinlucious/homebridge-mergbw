"""Run with: cd bridge && python -m pytest -q"""
import asyncio
import os

import pytest

os.environ.setdefault("MERGBW_MAC", "AA:BB:CC:DD:EE:FF")

import mergbw  # noqa: E402
import server  # noqa: E402


def valid(packet: bytes) -> bool:
    return sum(packet) & 0xFF == 0xFF


@pytest.mark.parametrize("hexstr", [
    "5601FF0600A3",  # power ack captured from a TG201A
    "5603FF0600A1",  # colour ack
    "560EFF060294",  # bind reply
])
def test_checksum_matches_real_captures(hexstr):
    assert valid(bytes.fromhex(hexstr))


def test_build_packet():
    assert mergbw.build_packet(0x01, b"\x01").hex() == "5501ff0601a3"
    assert mergbw.build_packet(0x00).hex() == "5500ff05a6"
    assert valid(mergbw.build_packet(0x03, b"\x00\xf0\x03\xe8"))


def test_colour_payloads():
    # HSV types: hue u16be degrees, saturation u16be 0-1000
    assert mergbw.color_payload(1, 0, 0, 255, False) == bytes([0, 240, 3, 232])
    # RGB types send the bytes as-is
    assert mergbw.color_payload(6, 1, 2, 3, False) == bytes([1, 2, 3])
    assert mergbw.color_payload(1, 1, 2, 3, True) == bytes([1, 2, 3])


def test_brightness_payloads():
    assert mergbw.brightness_payload(1, 50) == bytes([0x02, 0x26])  # (50+5)*10
    assert mergbw.brightness_payload(6, 50) == bytes([55])
    assert mergbw.brightness_payload(1, 150) == mergbw.brightness_payload(1, 100)


def test_parse_color():
    assert mergbw.parse_color("#ff0080") == (255, 0, 128)
    assert mergbw.parse_color("10,20,30") == (10, 20, 30)
    assert mergbw.parse_color("red") == (255, 0, 0)


class FakeLight:
    def __init__(self):
        self.calls = []
        self.is_connected = False

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    def __getattr__(self, name):
        async def record(*args):
            self.calls.append((name, *args))
        return record


def test_hue_and_saturation_writes_are_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / "state.json")

    async def run():
        ctl = server.Controller()
        ctl.light = FakeLight()
        await asyncio.gather(ctl.update({"hue": 120}), ctl.update({"saturation": 50}))
        return ctl

    ctl = asyncio.run(run())
    colour_calls = [c for c in ctl.light.calls if c[0] == "color_hs"]
    assert colour_calls == [("color_hs", 120, 50)]
    assert ctl.state["on"] is True
    assert (tmp_path / "state.json").exists()


def test_turning_off_sends_only_power(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / "state.json")

    async def run():
        ctl = server.Controller()
        ctl.light = FakeLight()
        await ctl.update({"on": False, "brightness": 30})
        return ctl

    ctl = asyncio.run(run())
    assert ctl.light.calls == [("power", False)]
    assert ctl.state["on"] is False
