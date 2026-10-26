'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const { EventEmitter } = require('node:events');
const hap = require('hap-nodejs');

// Minimal stand-in for the Homebridge API; hap.Accessory provides the same
// service methods as Homebridge's PlatformAccessory.
function loadPlatform(config) {
  const api = new EventEmitter();
  Object.assign(api, {
    hap,
    platformAccessory: function (name, uuid) {
      return new hap.Accessory(name, uuid);
    },
    registerPlatform(_plugin, _name, ctor) {
      api.Platform = ctor;
    },
    registerPlatformAccessories(_plugin, _name, accessories) {
      api.registered = accessories;
    },
    updatePlatformAccessories() {},
    unregisterPlatformAccessories() {},
  });
  delete require.cache[require.resolve('../index.js')];
  require('../index.js')(api);
  const log = Object.assign(() => {}, { info() {}, warn() {}, error() {}, debug() {} });
  const platform = new api.Platform(log, { platform: 'MeRGBW', pollInterval: 3600, ...config }, api);
  api.emit('didFinishLaunching');
  platform.registered = api.registered;
  return platform;
}

function fakeBridge(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler).listen(0, '127.0.0.1', () => {
      resolve({ server, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

const json = (res, body, status = 200) => {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test('exposes a single colour lightbulb', async () => {
  const p = loadPlatform({ url: 'http://127.0.0.1:1' });
  const { Service, Characteristic } = hap;
  const [accessory] = p.registered;
  const services = accessory.services.filter((s) => s.UUID !== Service.AccessoryInformation.UUID);
  assert.equal(services.length, 1);
  assert.equal(services[0].UUID, Service.Lightbulb.UUID);
  for (const c of [Characteristic.On, Characteristic.Brightness, Characteristic.Hue, Characteristic.Saturation]) {
    assert.ok(p.light.testCharacteristic(c));
  }
  await sleep(50);
});

test('forwards writes to the bridge and applies the returned state', async () => {
  const posts = [];
  const { server, url } = await fakeBridge((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (req.method === 'POST') {
        posts.push(JSON.parse(body));
      }
      json(res, { on: true, brightness: 42, hue: 10, saturation: 20 });
    });
  });
  const p = loadPlatform({ url });
  await p.light.getCharacteristic(hap.Characteristic.Brightness).handleSetRequest(42);
  assert.deepEqual(posts, [{ brightness: 42 }]);
  assert.equal(p.state.brightness, 42);
  server.close();
});

test('answers HomeKit within 5s even if the bridge is slow', async () => {
  const { server, url } = await fakeBridge((req, res) => {
    setTimeout(() => json(res, { on: true, brightness: 100, hue: 0, saturation: 0 }),
      req.method === 'POST' ? 7000 : 0);
  });
  const p = loadPlatform({ url });
  const start = Date.now();
  await p.light.getCharacteristic(hap.Characteristic.On).handleSetRequest(true);
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 5500, `took ${elapsed}ms`);
  assert.equal(p.state.on, true);
  server.closeAllConnections();
  server.close();
});

test('restores the last known state when a write fails', async () => {
  const { server, url } = await fakeBridge((req, res) => {
    if (req.method === 'POST') {
      return json(res, { error: 'light not found' }, 503);
    }
    json(res, { on: false, brightness: 50, hue: 0, saturation: 0 });
  });
  const p = loadPlatform({ url });
  await sleep(100); // initial poll
  await p.light.getCharacteristic(hap.Characteristic.On).handleSetRequest(true);
  await sleep(100);
  assert.equal(p.state.on, false);
  server.close();
});
