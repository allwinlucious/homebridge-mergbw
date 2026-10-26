'use strict';

// Exposes one MeRGBW light to HomeKit as a plain colour bulb
// (on / brightness / hue / saturation) through the local mergbw bridge
// (bridge/ in this repo), which owns the Bluetooth connection.

const PLUGIN_NAME = 'homebridge-mergbw';
const PLATFORM_NAME = 'MeRGBW';

module.exports = (api) => {
  api.registerPlatform(PLUGIN_NAME, PLATFORM_NAME, MeRGBWPlatform);
};

class MeRGBWPlatform {
  constructor(log, config, api) {
    this.log = log;
    this.config = config || {};
    this.api = api;
    this.cached = [];

    this.name = this.config.name || 'LED Strip';
    this.url = (this.config.url || 'http://127.0.0.1:8790').replace(/\/$/, '');
    this.pollMs = Math.max(2, this.config.pollInterval || 5) * 1000;
    this.state = { on: false, brightness: 100, hue: 0, saturation: 0 };

    api.on('didFinishLaunching', () => this.setup());
  }

  configureAccessory(accessory) {
    this.cached.push(accessory);
  }

  setup() {
    const { Service, Characteristic, uuid } = this.api.hap;
    const id = uuid.generate(`mergbw:${this.url}`);

    // drop cached accessories that no longer match this config
    const stale = this.cached.filter((a) => a.UUID !== id);
    if (stale.length) {
      this.api.unregisterPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, stale);
    }

    let accessory = this.cached.find((a) => a.UUID === id);
    const isNew = !accessory;
    if (isNew) {
      accessory = new this.api.platformAccessory(this.name, id);
    }

    accessory.getService(Service.AccessoryInformation)
      .setCharacteristic(Characteristic.Manufacturer, 'MeRGBW')
      .setCharacteristic(Characteristic.Model, this.config.model || 'MeRGBW Light')
      .setCharacteristic(Characteristic.SerialNumber, this.config.mac || 'mergbw');

    // remove any extra services left in the cache by older versions
    for (const service of [...accessory.services]) {
      if (service.UUID !== Service.AccessoryInformation.UUID && service.subtype !== 'main') {
        accessory.removeService(service);
      }
    }

    this.light = accessory.getServiceById(Service.Lightbulb, 'main')
      || accessory.addService(Service.Lightbulb, this.name, 'main');
    this.light.getCharacteristic(Characteristic.On)
      .onGet(() => this.state.on)
      .onSet((v) => this.send({ on: !!v }));
    this.light.getCharacteristic(Characteristic.Brightness)
      .onGet(() => this.state.brightness)
      .onSet((v) => this.send({ brightness: v }));
    this.light.getCharacteristic(Characteristic.Hue)
      .onGet(() => this.state.hue)
      .onSet((v) => this.send({ hue: v }));
    this.light.getCharacteristic(Characteristic.Saturation)
      .onGet(() => this.state.saturation)
      .onSet((v) => this.send({ saturation: v }));

    // register only once the service exists so HomeKit sees the full accessory
    if (isNew) {
      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, [accessory]);
    } else {
      this.api.updatePlatformAccessories([accessory]);
    }

    this.poll();
    setInterval(() => this.poll(), this.pollMs).unref();
  }

  async request(method, path, body) {
    const res = await fetch(this.url + path, {
      method,
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(method === 'GET' ? 3000 : 30000),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  async send(changes) {
    // HomeKit marks the accessory "No Response" if a write takes too long, and a
    // cold BLE reconnect can be slow when the strip is busy. Answer HomeKit after
    // SET_TIMEOUT_MS and let the request finish in the background; if it fails,
    // the next poll puts the tile back to the real state.
    const SET_TIMEOUT_MS = 5000;
    this.state = { ...this.state, ...changes };
    this.inflight = (this.inflight || 0) + 1;
    const pending = this.request('POST', '/state', changes)
      .then((state) => this.apply(state))
      .catch((e) => {
        this.log.error(`Failed to set ${JSON.stringify(changes)}: ${e.message}`);
        if (this.lastKnown) {
          this.apply(this.lastKnown);
        }
      })
      .finally(() => {
        this.inflight -= 1;
        if (!this.inflight) {
          this.poll();
        }
      });
    await Promise.race([pending, new Promise((r) => setTimeout(r, SET_TIMEOUT_MS))]);
  }

  async poll() {
    try {
      const state = await this.request('GET', '/state');
      // a write in flight would be overwritten with the pre-write state
      if (!this.inflight) {
        this.apply(state);
      }
      if (this.offline) {
        this.log.info('mergbw bridge reachable again');
        this.offline = false;
      }
    } catch (e) {
      if (!this.offline) {
        this.log.warn(`mergbw bridge unreachable at ${this.url}: ${e.message}`);
        this.offline = true;
      }
    }
  }

  apply(state) {
    const { Characteristic } = this.api.hap;
    this.lastKnown = state;
    this.state = { ...state };
    this.light.updateCharacteristic(Characteristic.On, state.on);
    this.light.updateCharacteristic(Characteristic.Brightness, state.brightness);
    this.light.updateCharacteristic(Characteristic.Hue, state.hue);
    this.light.updateCharacteristic(Characteristic.Saturation, state.saturation);
  }
}
