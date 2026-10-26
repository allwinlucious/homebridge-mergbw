# homebridge-mergbw

[![npm](https://img.shields.io/npm/v/homebridge-mergbw)](https://www.npmjs.com/package/homebridge-mergbw)
[![test](https://github.com/allwinlucious/homebridge-mergbw/actions/workflows/test.yml/badge.svg)](https://github.com/allwinlucious/homebridge-mergbw/actions/workflows/test.yml)
[![license](https://img.shields.io/github/license/allwinlucious/homebridge-mergbw)](LICENSE)

Control **MeRGBW** Bluetooth LED strips from **Apple Home** with [Homebridge](https://homebridge.io).

MeRGBW lights are cheap Bluetooth LE strips, panels and lamps that normally only work
with the *MeRGBW* phone app. This project adds them to HomeKit as a normal colour bulb:
on/off, brightness and colour, plus Siri, scenes and automations.

<!-- Photos: add your images to docs/images/ and uncomment.
<p align="center">
  <img src="docs/images/strip.jpg" width="48%" alt="MeRGBW LED strip">
  <img src="docs/images/home-app.png" width="48%" alt="The strip in the Apple Home app">
</p>
-->

## Contents

- [How it works](#how-it-works)
- [Supported devices](#supported-devices)
- [Requirements](#requirements)
- [Installation](#installation)
  - [1. Run the bridge](#1-run-the-bridge)
  - [2. Install the Homebridge plugin](#2-install-the-homebridge-plugin)
  - [3. Configure the plugin](#3-configure-the-plugin)
- [Running the bridge without Docker](#running-the-bridge-without-docker)
- [Bridge HTTP API](#bridge-http-api)
- [Command-line tool](#command-line-tool)
- [Troubleshooting](#troubleshooting)
- [Protocol notes](#protocol-notes)
- [Credits](#credits)

## How it works

```
Apple Home ──HomeKit──▶ Homebridge + homebridge-mergbw ──HTTP──▶ mergbw bridge ──Bluetooth LE──▶ LED strip
```

This project has two parts:

| Part | What it is | Where |
|---|---|---|
| **Homebridge plugin** | Exposes the light to HomeKit. Talks to the bridge over HTTP. | `homebridge-mergbw` on npm (this repo's root) |
| **Bridge** | Small Python service that owns the Bluetooth connection. | [`bridge/`](bridge/), Docker image on `ghcr.io` |

Bluetooth is handled by a separate service because Homebridge is often run in Docker or
on a different machine than the one near the light. Splitting them also lets the bridge:

- **share the light with the phone app.** These lights accept only one Bluetooth connection,
  so the bridge disconnects after 20 s of inactivity and reconnects on the next command (about 1.5 s).
- **remember the colour.** The lights can't report their colour, so the bridge stores what it last set.
- **keep HomeKit responsive.** Reads are answered from memory, and writes always answer
  HomeKit within 5 s, so the light doesn't show "No Response" while Bluetooth reconnects.

## Supported devices

The MeRGBW app controls several product families, identified by a device **type** that
the light advertises:

| Type | Device | Status |
|---|---|---|
| 1 | Addressable LED strip, hexagon / letter lights (e.g. **TG201A**) | ✅ Tested |
| 2 | RGB line / strip | Should work, untested |
| 3 | Star projector | Untested |
| 4 | Handle light | Untested |
| 5 | Sunset lamp | Untested |
| 6 | Bulb | Untested |
| 7, 8 | Ceiling lights | Untested |

If you have one of the untested devices, please
[open an issue](https://github.com/allwinlucious/homebridge-mergbw/issues) whether it works or not.

## Requirements

- A Linux machine with Bluetooth near the light (a Raspberry Pi 3/4/5 is ideal) and **BlueZ** installed
- **Docker** with Compose (recommended), or Python 3.10+
- **Homebridge** 1.6+ (or 2.0), on the same machine or anywhere on your network

## Installation

### 1. Run the bridge

On the machine with Bluetooth:

```bash
sudo apt install bluez            # if not installed already
git clone https://github.com/allwinlucious/homebridge-mergbw.git
cd homebridge-mergbw/bridge
cp .env.example .env
```

Close the MeRGBW app on your phone (the light allows only one connection), then find your light:

```bash
docker compose run --rm mergbw python mergbw.py scan
```

```
* AA:BB:CC:DD:EE:FF   -40 dBm  LED Lights           type=1 (Addressable strip / letters / hexagon) model=TG201A
```

Put the MAC address and type into `.env`:

```ini
MERGBW_MAC=AA:BB:CC:DD:EE:FF
MERGBW_TYPE=1
```

Start the bridge and check that it can talk to the light:

```bash
docker compose up -d
curl -s -X POST 127.0.0.1:8790/state -H 'content-type: application/json' -d '{"on":true,"hue":240,"saturation":100}'
```

The light should turn blue.

> **Note:** the container needs `/run/dbus` mounted and `apparmor:unconfined` to reach BlueZ.
> Both are already set in [`docker-compose.yml`](bridge/docker-compose.yml).

The prebuilt image [`ghcr.io/allwinlucious/homebridge-mergbw-bridge`](https://github.com/allwinlucious/homebridge-mergbw/pkgs/container/homebridge-mergbw-bridge)
is available for `amd64` (x86), `arm64` (64-bit Raspberry Pi OS) and `arm/v7` (32-bit).
To build it yourself instead, run `docker compose up -d --build`.

### 2. Install the Homebridge plugin

In the **Homebridge UI**, open **Plugins**, search for `homebridge-mergbw` and install it.

Or from the command line:

```bash
npm install -g homebridge-mergbw
```

If you run the official `homebridge/homebridge` Docker image:

```bash
docker exec <homebridge-container> sh -c 'cd /homebridge && npm install homebridge-mergbw'
```

### 3. Configure the plugin

Use the plugin's **Settings** in the Homebridge UI, or add a platform to `config.json`:

```json
{
  "platforms": [
    {
      "platform": "MeRGBW",
      "name": "LED Strip",
      "url": "http://127.0.0.1:8790"
    }
  ]
}
```

| Option | Default | Description |
|---|---|---|
| `name` | `LED Strip` | Name shown in the Home app |
| `url` | `http://127.0.0.1:8790` | Address of the bridge |
| `pollInterval` | `5` | Seconds between state refreshes from the bridge (local, doesn't touch Bluetooth) |
| `model` | `MeRGBW Light` | Model shown in the accessory details |
| `mac` | | Shown as the serial number in the accessory details |

Restart Homebridge. The light appears in the Home app under your Homebridge bridge.
No separate pairing is needed.

#### Homebridge on another machine

Run the bridge on a machine near the light, set `MERGBW_HOST=0.0.0.0` in its `.env`, and point
`url` at it (for example `http://192.0.2.10:8790`).

> **Warning:** the API has no authentication. Only expose it on a network you trust.

## Running the bridge without Docker

```bash
sudo mkdir -p /opt/mergbw-bridge
sudo cp bridge/mergbw.py bridge/server.py bridge/requirements.txt bridge/.env.example /opt/mergbw-bridge/
cd /opt/mergbw-bridge
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env     # set MERGBW_MAC and MERGBW_TYPE

sudo cp /path/to/homebridge-mergbw/bridge/mergbw-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mergbw-bridge
journalctl -u mergbw-bridge -f
```

## Bridge HTTP API

The plugin only uses on/off, brightness and colour. The bridge also supports the light's
built-in effects, which you can use from scripts or other home automation tools.

| Method | Path | Description |
|---|---|---|
| `GET` | `/state` | Current state |
| `POST` | `/state` | Change state (JSON body, any subset of the fields below) |
| `POST` | `/refresh` | Re-read power and brightness from the light |

| Field | Type | Description |
|---|---|---|
| `on` | bool | Power |
| `brightness` | 0–100 | Brightness |
| `hue` | 0–360 | Colour hue |
| `saturation` | 0–100 | Colour saturation |
| `scene` | int | Built-in effect (see below) |
| `speed` | 0–100 | Effect speed |
| `music` | 1–6 | Music-reactive mode (uses the controller's microphone) |
| `sensitivity` | 0–100 | Microphone sensitivity |
| `mode` | `"color"` | Leave scene/music mode and restore the solid colour |

Known scene indices on the TG201A: Symphony `2`, Energy `3`, Jump `4`, Party `6`,
Vitality `7`, Forest `13`, Chase `23`, Rainbow `26`, Neon `48`.

```bash
curl -s 127.0.0.1:8790/state
curl -s -X POST 127.0.0.1:8790/state -H 'content-type: application/json' -d '{"scene":26,"speed":70}'
curl -s -X POST 127.0.0.1:8790/state -H 'content-type: application/json' -d '{"mode":"color"}'
```

Bridge settings (in `.env`):

| Variable | Default | Description |
|---|---|---|
| `MERGBW_MAC` | (required) | MAC address of the light |
| `MERGBW_TYPE` | `1` | Device type from `scan` |
| `MERGBW_HOST` | `127.0.0.1` | Listen address |
| `MERGBW_PORT` | `8790` | Listen port |
| `MERGBW_IDLE_TIMEOUT` | `20` | Seconds before releasing the Bluetooth connection |
| `MERGBW_POLL_INTERVAL` | `300` | Seconds between re-reading power/brightness from the light (`0` = never) |

## Command-line tool

[`bridge/mergbw.py`](bridge/mergbw.py) also works on its own, without Homebridge.
Stop the bridge first, because only one connection is allowed at a time.

```bash
python mergbw.py scan                 # find lights; remembers the nearest one
python mergbw.py on
python mergbw.py color ff0080         # or: color red / color 255,0,128
python mergbw.py brightness 40
python mergbw.py scene 26 --speed 70
python mergbw.py status
python mergbw.py -v raw 0x02          # send a raw command and print the reply
python mergbw.py --help
```

With Docker: `docker compose run --rm mergbw python mergbw.py <command>`.

## Troubleshooting

**`scan` finds nothing, or the bridge logs "Light … not found"**
Close the MeRGBW app on your phone and any other app connected to the light. Make sure the
light is powered and within Bluetooth range of the machine. Run `scan --all` to list every
Bluetooth device nearby.

**`AccessDenied … AppArmor policy prevents this sender`**
Docker's default AppArmor profile is blocking D-Bus. Keep `security_opt: [apparmor:unconfined]`
in `docker-compose.yml`.

**Colours look wrong**
Check that `MERGBW_TYPE` matches the type reported by `scan`. Types 1, 2, 4 and 7 take colour
as hue/saturation; the others take RGB.

**The phone app can't connect**
The bridge holds the connection for `MERGBW_IDLE_TIMEOUT` seconds after each command. Wait
for it to time out, or lower the value.

**Logs**
`docker compose logs -f` in `bridge/` for the bridge, and the Homebridge log for the plugin.

## Protocol notes

The lights use a simple protocol over GATT service `FFF0`. Commands are written to
characteristic `FFF3` (with response), and replies arrive as notifications on `FFF4`:

```
55 CMD FF LEN PAYLOAD… CHECKSUM      LEN = whole packet length, CHECKSUM = ~sum(bytes) & 0xFF
```

| CMD | Meaning | Payload |
|---|---|---|
| `00` | Status | — (reply: power, brightness, timers, LED count, …) |
| `01` | Power | `00` / `01` |
| `03` | Colour | types 1/2/4/7: hue u16, saturation×10 u16; others: R G B |
| `05` | Brightness | types 1/2/4: (pct+5)×10 as u16; others: pct+5 |
| `06` | Scene | index |
| `07` | Music mode | 1–6 |
| `0F` | Effect speed | 0–100 |

See [`bridge/mergbw.py`](bridge/mergbw.py) for the full list.

## Credits

The protocol was reverse-engineered by others. This project builds on their work:

- [launxy/MeRGBW-Linux](https://github.com/launxy/MeRGBW-Linux): a port of the Android app's protocol code, with notes
- [rexxars/ha-mergbw](https://github.com/rexxars/ha-mergbw): Home Assistant integration

Not affiliated with or endorsed by MeRGBW or its manufacturer.

## License

[MIT](LICENSE)
