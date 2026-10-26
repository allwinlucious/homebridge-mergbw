#!/usr/bin/env python3
"""Control MeRGBW Bluetooth LED strips/lights from Linux (Raspberry Pi).

Protocol (from community reverse-engineering of the MeRGBW Android app,
see github.com/launxy/MeRGBW-Linux and github.com/rexxars/ha-mergbw):

    packet   = [0x55, CMD, 0xFF, LEN, PAYLOAD..., CHECKSUM]
    LEN      = 5 + len(payload)            (whole packet incl. checksum)
    CHECKSUM = ~sum(previous bytes) & 0xFF
    GATT     : service FFF0, write FFF3, notify FFF4 (replies start with 0x56)

Usage examples:
    mergbw.py scan                      # find lights, remembers the first one
    mergbw.py on | off
    mergbw.py color ff0080              # or: color red / color 255,0,128
    mergbw.py brightness 40             # 0-100
    mergbw.py scene 3 --speed 60
    mergbw.py status
    mergbw.py --mac AA:BB:CC:DD:EE:FF --type 1 on
"""
from __future__ import annotations

import argparse
import asyncio
import colorsys
import json
import sys
from pathlib import Path

from bleak import BleakClient, BleakScanner

WRITE_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000fff4-0000-1000-8000-00805f9b34fb"
ADV_UUID_FRAGMENT = "3519"  # 16-bit service UUID 0x3519 in advertisements

CMD_SYNC_STATUS = 0x00
CMD_POWER = 0x01
CMD_FIRMWARE = 0x02
CMD_COLOR = 0x03
CMD_BRIGHTNESS = 0x05
CMD_MODE = 0x06
CMD_MUSIC_MODE = 0x07
CMD_MUSIC_SENS = 0x08
CMD_LED_NUM = 0x0B
CMD_SYNC_TIME = 0x0C
CMD_MODE_SPEED = 0x0F

DEVICE_TYPES = {
    1: "Addressable strip / letters / hexagon",
    2: "RGB line/strip",
    3: "Star projector",
    4: "Handle light",
    5: "Sunset lamp",
    6: "Bulb",
    7: "Ceiling (colorful)",
    8: "Ceiling (RGB)",
}
HSV_COLOR_TYPES = {1, 2, 4, 7}
TWO_BYTE_BRIGHTNESS_TYPES = {1, 2, 4}

NAMED_COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "yellow": (255, 255, 0), "cyan": (0, 255, 255),
    "magenta": (255, 0, 255), "purple": (128, 0, 255), "orange": (255, 100, 0),
    "pink": (255, 60, 150), "warm": (255, 180, 100),
}

CONFIG_PATH = Path.home() / ".config" / "mergbw.json"


# ---------------------------------------------------------------- protocol

def build_packet(cmd: int, payload: bytes = b"") -> bytes:
    pkt = bytes([0x55, cmd, 0xFF, 5 + len(payload)]) + payload
    return pkt + bytes([(~sum(pkt)) & 0xFF])


def color_payload(dev_type: int, r: int, g: int, b: int, force_rgb: bool) -> bytes:
    if dev_type in HSV_COLOR_TYPES and not force_rgb:
        h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        hue, sat = int(h * 360) % 360, int(s * 1000)
        return bytes([hue >> 8, hue & 0xFF, sat >> 8, sat & 0xFF])
    return bytes([r, g, b])


def brightness_payload(dev_type: int, pct: int) -> bytes:
    pct = max(0, min(100, pct))
    if dev_type in TWO_BYTE_BRIGHTNESS_TYPES:
        v = (pct + 5) * 10
        return bytes([v >> 8, v & 0xFF])
    return bytes([pct + 5])


def parse_color(text: str) -> tuple[int, int, int]:
    t = text.strip().lower().lstrip("#")
    if t in NAMED_COLORS:
        return NAMED_COLORS[t]
    if "," in t:
        r, g, b = (int(x) for x in t.split(","))
        return r, g, b
    if len(t) == 6:
        return int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
    raise ValueError(f"can't parse colour '{text}' (use ff0080, 255,0,128 or a name)")


def parse_manufacturer(data: bytes) -> tuple[int | None, str | None]:
    """bleak strips the 2-byte company id, so type is at [8], model at [9:19]."""
    if len(data) < 9 or not 1 <= data[8] <= 8:
        return None, None
    model = bytes(data[9:19]).split(b"\x00")[0].decode(errors="ignore").strip() or None
    return data[8], model


# ---------------------------------------------------------------- config

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------- BLE

async def scan(timeout: float, show_all: bool) -> list[dict]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for dev, adv in found.values():
        uuids = [str(u).lower() for u in adv.service_uuids]
        dev_type = model = None
        for data in adv.manufacturer_data.values():
            dev_type, model = parse_manufacturer(bytes(data))
            if dev_type:
                break
        likely = any(ADV_UUID_FRAGMENT in u for u in uuids) or (
            dev_type is not None and model is not None)
        if likely or show_all:
            results.append({"mac": dev.address, "name": (adv.local_name or dev.name or "").strip("\x00 "),
                            "rssi": adv.rssi, "type": dev_type, "model": model, "likely": likely})
    return sorted(results, key=lambda d: (not d["likely"], -d["rssi"]))


class Light:
    def __init__(self, mac: str, dev_type: int, verbose: bool = False):
        self.mac, self.type, self.verbose = mac, dev_type, verbose
        self.client: BleakClient | None = None
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    async def connect(self) -> None:
        dev = await BleakScanner.find_device_by_address(self.mac, timeout=10)
        if dev is None:
            raise ConnectionError(f"Light {self.mac} not found. Is it powered, in range, "
                                  "and disconnected from the phone app?")
        self.client = BleakClient(dev)
        await self.client.connect()
        await self.client.start_notify(NOTIFY_CHAR, self._on_notify)
        await asyncio.sleep(0.3)

    async def disconnect(self) -> None:
        if self.is_connected:
            await self.client.disconnect()
        self.client = None

    async def __aenter__(self):
        try:
            await self.connect()
        except ConnectionError as e:
            raise SystemExit(str(e))
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    def _on_notify(self, _sender, data: bytearray):
        if self.verbose:
            print(f"  <- {bytes(data).hex(' ')}")
        self._replies.put_nowait(bytes(data))

    async def send(self, cmd: int, payload: bytes = b"") -> None:
        pkt = build_packet(cmd, payload)
        if self.verbose:
            print(f"  -> {pkt.hex(' ')}")
        try:
            # Telink firmware in these lights only reacts reliably to write-with-response
            await self.client.write_gatt_char(WRITE_CHAR, pkt, response=True)
        except Exception:
            await self.client.write_gatt_char(WRITE_CHAR, pkt, response=False)
        await asyncio.sleep(0.15)

    async def request(self, cmd: int, payload: bytes = b"", timeout: float = 2.0) -> bytes | None:
        while not self._replies.empty():
            self._replies.get_nowait()
        await self.send(cmd, payload)
        try:
            while True:
                data = await asyncio.wait_for(self._replies.get(), timeout)
                if len(data) > 1 and data[1] == cmd:
                    return data
        except asyncio.TimeoutError:
            return None

    async def power(self, on: bool):
        await self.send(CMD_POWER, bytes([1 if on else 0]))

    async def color(self, r: int, g: int, b: int, force_rgb: bool = False):
        await self.send(CMD_COLOR, color_payload(self.type, r, g, b, force_rgb))

    async def color_hs(self, hue: float, sat: float, force_rgb: bool = False):
        """Hue 0-360, saturation 0-100 (HomeKit's native units)."""
        if self.type in HSV_COLOR_TYPES and not force_rgb:
            h, s = int(hue) % 360, int(max(0, min(100, sat)) * 10)
            await self.send(CMD_COLOR, bytes([h >> 8, h & 0xFF, s >> 8, s & 0xFF]))
        else:
            r, g, b = colorsys.hsv_to_rgb(hue / 360, sat / 100, 1)
            await self.send(CMD_COLOR, bytes([int(r * 255), int(g * 255), int(b * 255)]))

    async def brightness(self, pct: int):
        await self.send(CMD_BRIGHTNESS, brightness_payload(self.type, pct))

    async def scene(self, index: int, speed: int | None = None):
        payload = bytes([index >> 8, index & 0xFF]) if index > 255 else bytes([index])
        await self.send(CMD_MODE, payload)
        if speed is not None:
            await self.send(CMD_MODE_SPEED, bytes([max(0, min(100, speed))]))

    async def music(self, mode: int):
        await self.send(CMD_MUSIC_MODE, bytes([mode & 0xFF]))

    async def music_sensitivity(self, pct: int):
        pct = max(0, min(100, pct))
        await self.send(CMD_MUSIC_SENS, bytes([int(pct / 100 * 40 + 60)]))

    async def status(self) -> dict | None:
        d = await self.request(CMD_SYNC_STATUS)
        if not d or len(d) < 6:
            return None
        out = {"on": d[4] == 1, "raw": d.hex(" ")}
        if self.type in TWO_BYTE_BRIGHTNESS_TYPES and len(d) >= 7:
            out["brightness"] = ((d[5] << 8 | d[6]) // 10) - 5
            if len(d) >= 17:
                out["led_count"] = d[15] << 8 | d[16]
        else:
            out["brightness"] = d[5] - 5
        return out


# ---------------------------------------------------------------- CLI

async def main() -> int:
    ap = argparse.ArgumentParser(description="Control MeRGBW Bluetooth lights")
    ap.add_argument("--mac", help="light MAC address (default: saved from last scan)")
    ap.add_argument("--type", type=int, choices=range(1, 9), metavar="1-8",
                    help="device type (default: auto-detected at scan, else 1)")
    ap.add_argument("--rgb-mode", action="store_true",
                    help="send colour as raw RGB instead of HSV (try if colours look wrong)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print raw packets")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="find MeRGBW lights and save the nearest one")
    p.add_argument("--timeout", type=float, default=8)
    p.add_argument("--all", action="store_true", help="list every BLE device")
    sub.add_parser("on")
    sub.add_parser("off")
    p = sub.add_parser("color", help="set colour: ff0080 | 255,0,128 | red")
    p.add_argument("value")
    p = sub.add_parser("brightness", help="0-100")
    p.add_argument("value", type=int)
    p = sub.add_parser("scene", help="built-in effect by index")
    p.add_argument("index", type=int)
    p.add_argument("--speed", type=int, help="0-100")
    p = sub.add_parser("music", help="music-reactive mode (uses the controller's mic)")
    p.add_argument("mode", type=int)
    p = sub.add_parser("ledcount", help="set number of LEDs (addressable strips)")
    p.add_argument("value", type=int)
    sub.add_parser("status")
    sub.add_parser("synctime", help="set the light's clock to the Pi's time")
    p = sub.add_parser("raw", help="send raw command: raw <cmd> [payload hex]")
    p.add_argument("command", type=lambda s: int(s, 0))
    p.add_argument("payload", nargs="?", default="")
    args = ap.parse_args()

    cfg = load_config()

    if args.cmd == "scan":
        devices = await scan(args.timeout, args.all)
        if not devices:
            print("No MeRGBW lights found. Close the phone app (lights allow one connection) "
                  "and try again, or use 'scan --all'.")
            return 1
        for d in devices:
            mark = "*" if d["likely"] else " "
            tname = DEVICE_TYPES.get(d["type"], "unknown type")
            print(f"{mark} {d['mac']}  {d['rssi']:>4} dBm  {d['name'] or '?':<20} "
                  f"type={d['type']} ({tname}) model={d['model']}")
        best = next((d for d in devices if d["likely"]), None)
        if best:
            save_config({"mac": best["mac"], "type": best["type"] or 1})
            print(f"\nSaved {best['mac']} (type {best['type'] or 1}) as default in {CONFIG_PATH}")
        return 0

    mac = args.mac or cfg.get("mac")
    if not mac:
        print("No light known yet: run 'scan' first or pass --mac.")
        return 1
    dev_type = args.type or (cfg.get("type") if mac == cfg.get("mac") else None) or 1
    if args.mac and (args.mac != cfg.get("mac") or args.type):
        save_config({"mac": args.mac, "type": dev_type})

    async with Light(mac, dev_type, args.verbose) as light:
        if args.cmd == "on":
            await light.power(True)
        elif args.cmd == "off":
            await light.power(False)
        elif args.cmd == "color":
            await light.color(*parse_color(args.value), force_rgb=args.rgb_mode)
        elif args.cmd == "brightness":
            await light.brightness(args.value)
        elif args.cmd == "scene":
            await light.scene(args.index, args.speed)
        elif args.cmd == "music":
            await light.music(args.mode)
        elif args.cmd == "ledcount":
            await light.send(CMD_LED_NUM, bytes([args.value >> 8, args.value & 0xFF]))
        elif args.cmd == "synctime":
            from datetime import datetime
            n = datetime.now()
            await light.send(CMD_SYNC_TIME, bytes([n.year >> 8, n.year & 0xFF, n.month, n.day,
                                                   n.hour, n.minute, n.second, n.isoweekday()]))
        elif args.cmd == "status":
            st = await light.status()
            if st is None:
                print("No status reply (some firmwares don't answer; commands may still work).")
            else:
                print(json.dumps(st, indent=2))
        elif args.cmd == "raw":
            reply = await light.request(args.command, bytes.fromhex(args.payload))
            print("reply:", reply.hex(" ") if reply else None)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
