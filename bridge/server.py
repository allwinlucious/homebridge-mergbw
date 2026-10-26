#!/usr/bin/env python3
"""Small HTTP bridge for one MeRGBW light, used by the homebridge-mergbw plugin.

The light can't report its colour/scene, so this service remembers what it
last set and persists it to STATE_FILE. Power/brightness are re-read from the
light every POLL_INTERVAL seconds to catch changes made with the phone app or
the remote. The BLE connection is dropped after IDLE_TIMEOUT seconds so the
phone app can still connect (the light accepts only one connection).

API (JSON):
    GET  /state   -> {"on", "brightness", "hue", "saturation", "mode",
                      "scene", "speed", "music", "sensitivity", "connected", "error"}
    POST /state   <- any subset of: on (bool), brightness (0-100), hue (0-360),
                     saturation (0-100), scene (index), speed (0-100),
                     music (mode), sensitivity (0-100), mode ("color" to leave
                     scene/music mode and restore the solid colour)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from mergbw import Light

MAC = os.environ["MERGBW_MAC"]
DEVICE_TYPE = int(os.environ.get("MERGBW_TYPE", "1"))
HOST = os.environ.get("MERGBW_HOST", "127.0.0.1")
PORT = int(os.environ.get("MERGBW_PORT", "8790"))
IDLE_TIMEOUT = float(os.environ.get("MERGBW_IDLE_TIMEOUT", "20"))
POLL_INTERVAL = float(os.environ.get("MERGBW_POLL_INTERVAL", "300"))
STATE_FILE = Path(os.environ.get("MERGBW_STATE_FILE", "/data/state.json"))
DEBOUNCE = 0.15  # HomeKit sends hue and saturation as separate writes

log = logging.getLogger("mergbw")


class Controller:
    def __init__(self):
        self.light = Light(MAC, DEVICE_TYPE)
        self.lock = asyncio.Lock()
        self.last_used = 0.0
        self.error: str | None = None
        self.state = {"on": False, "brightness": 100, "hue": 0, "saturation": 0,
                      "mode": "color", "scene": None, "speed": 50, "music": None,
                      "sensitivity": 80}
        try:
            self.state.update(json.loads(STATE_FILE.read_text()))
        except (OSError, ValueError):
            pass
        self._pending: dict = {}
        self._flush_task: asyncio.Task | None = None
        self._next_flush: asyncio.Task | None = None

    def snapshot(self) -> dict:
        return {**self.state, "connected": self.light.is_connected, "error": self.error}

    def _save(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.state))
        except OSError as e:
            log.warning("could not save state: %s", e)

    async def _ensure_connected(self):
        if not self.light.is_connected:
            log.info("connecting to %s", MAC)
            await self.light.connect()

    async def _run(self, fn):
        """Run fn(light) under the lock, reconnecting once on failure."""
        async with self.lock:
            for attempt in (1, 2):
                try:
                    await self._ensure_connected()
                    result = await fn(self.light)
                    self.last_used = time.monotonic()
                    self.error = None
                    return result
                except Exception as e:
                    log.warning("BLE operation failed (attempt %d): %s", attempt, e)
                    self.error = str(e)
                    await self.light.disconnect()
            raise ConnectionError(self.error)

    async def update(self, changes: dict) -> dict:
        # Changes wait in _pending until the queued flush task picks them up;
        # that task waits for any flush still talking to the light first.
        self._pending.update(changes)
        if self._next_flush is None:
            self._next_flush = asyncio.create_task(self._flush(self._flush_task))
        await asyncio.shield(self._next_flush)
        return self.snapshot()

    async def _flush(self, previous: asyncio.Task | None):
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        await asyncio.sleep(DEBOUNCE)
        self._flush_task, self._next_flush = self._next_flush, None
        ch, self._pending = self._pending, {}
        s = self.state

        async def apply(light: Light):
            if ch.get("on") is False:
                await light.power(False)
                s["on"] = False
                return
            if ch.get("on") is True or any(k in ch for k in ("brightness", "hue", "saturation",
                                                            "scene", "music", "mode")):
                # always resend: cached power state may be stale (phone app / remote)
                await light.power(True)
                s["on"] = True
            if "hue" in ch or "saturation" in ch or ch.get("mode") == "color":
                s["hue"] = ch.get("hue", s["hue"])
                s["saturation"] = ch.get("saturation", s["saturation"])
                await light.color_hs(s["hue"], s["saturation"])
                s.update(mode="color", scene=None, music=None)
            if "scene" in ch:
                s.update(mode="scene", scene=int(ch["scene"]), music=None)
                s["speed"] = int(ch.get("speed", s["speed"]))
                await light.scene(s["scene"], s["speed"])
            elif "speed" in ch:
                s["speed"] = int(ch["speed"])
                if s["mode"] == "scene":
                    await light.scene(s["scene"], s["speed"])
            if "sensitivity" in ch:
                s["sensitivity"] = int(ch["sensitivity"])
                await light.music_sensitivity(s["sensitivity"])
            if "music" in ch:
                s.update(mode="music", music=int(ch["music"]), scene=None)
                await light.music(s["music"])
                await light.music_sensitivity(s["sensitivity"])
            if "brightness" in ch:
                s["brightness"] = int(ch["brightness"])
                await light.brightness(s["brightness"])

        try:
            await self._run(apply)
        finally:
            self._save()

    async def refresh(self):
        st = await self._run(lambda light: light.status())
        if st:
            self.state["on"] = st["on"]
            self.state["brightness"] = max(0, min(100, st["brightness"]))
            self._save()

    async def housekeeping(self):
        last_poll = 0.0
        while True:
            await asyncio.sleep(2)
            now = time.monotonic()
            if self.light.is_connected and not self.lock.locked() and now - self.last_used > IDLE_TIMEOUT:
                async with self.lock:
                    log.info("idle, disconnecting")
                    await self.light.disconnect()
            if POLL_INTERVAL > 0 and now - last_poll > POLL_INTERVAL and now - self.last_used > IDLE_TIMEOUT:
                last_poll = now
                try:
                    await self.refresh()
                    async with self.lock:
                        await self.light.disconnect()
                except Exception as e:
                    log.info("status poll failed: %s", e)


def make_app(ctl: Controller) -> web.Application:
    async def get_state(_req):
        return web.json_response(ctl.snapshot())

    async def post_state(req):
        try:
            body = await req.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        allowed = {"on", "brightness", "hue", "saturation", "scene", "speed", "music",
                   "sensitivity", "mode"}
        changes = {k: v for k, v in body.items() if k in allowed}
        try:
            return web.json_response(await ctl.update(changes))
        except ConnectionError as e:
            return web.json_response({**ctl.snapshot(), "error": str(e)}, status=503)

    async def post_refresh(_req):
        try:
            await ctl.refresh()
            return web.json_response(ctl.snapshot())
        except ConnectionError as e:
            return web.json_response({**ctl.snapshot(), "error": str(e)}, status=503)

    async def start_bg(app):
        app["housekeeping"] = asyncio.create_task(ctl.housekeeping())

    async def cleanup(app):
        app["housekeeping"].cancel()
        await ctl.light.disconnect()

    app = web.Application()
    app.add_routes([web.get("/state", get_state), web.post("/state", post_state),
                    web.post("/refresh", post_refresh)])
    app.on_startup.append(start_bg)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("MERGBW_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("MeRGBW bridge for %s (type %d) on %s:%d", MAC, DEVICE_TYPE, HOST, PORT)
    web.run_app(make_app(Controller()), host=HOST, port=PORT, print=None, access_log=None)
