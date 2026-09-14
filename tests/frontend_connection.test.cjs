const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function harness(fetchHandler) {
    const elements = new Map();
    const alerts = [];
    function element(id) {
        if (!elements.has(id)) elements.set(id, {
            style: {}, value: '', textContent: '', disabled: false,
            classList: { toggle() {} }, addEventListener() {},
        });
        return elements.get(id);
    }
    const instances = [];
    class FakeRFB {
        constructor(target, url, options) {
            assert.ok(target);
            this.target = target;
            this.url = url;
            this.options = options;
            this.events = {};
            instances.push(this);
        }
        addEventListener(type, fn) { this.events[type] = fn; }
        disconnect() {}
        sendCredentials() {}
    }
    const context = vm.createContext({
        document: { getElementById: element, querySelector: element, addEventListener() {} },
        fetch: (...args) => fetchHandler(...args),
        location: { protocol: 'http:', hostname: 'localhost', host: 'localhost:5011' },
        alert: message => alerts.push(message), prompt: () => '', console,
        FakeRFB,
    });
    vm.runInContext(script, context);
    vm.runInContext('RFBConstructor = FakeRFB; addMessage = () => {};', context);
    return { context, element, instances, alerts, run: expression => vm.runInContext(expression, context) };
}

function response(data, ok = true) { return { ok, json: async () => data }; }
const info = (port = 6082) => response({ running: true, ws_port: port, vnc_password: 'test-only' });

test('screenshot rendering preserves the VNC container on success and failure', async () => {
    let result = response({ success: true, screenshot: 'data:image/png;base64,test' });
    const h = harness(async () => result);
    // Replacing desktopView.innerHTML is exactly the reported regression.
    Object.defineProperty(h.element('desktopView'), 'innerHTML', {
        set() { throw new Error('VNC target must not be deleted'); },
    });
    const target = h.element('vncCanvas');
    await h.run('refreshScreenshot()');
    assert.equal(h.element('screenshotFallback').style.display, 'block');
    result = response({ success: false, error: 'test failure' });
    await h.run('refreshScreenshot()');
    assert.equal(h.element('vncPlaceholder').style.display, 'block');
    assert.equal(h.element('vncCanvas'), target);
});

test('connection waits for configured port and uses the latest port on reconnect', async () => {
    let resolveInfo;
    const h = harness(() => new Promise(resolve => { resolveInfo = resolve; }));
    const pending = h.run('connectVNC()');
    assert.equal(h.instances.length, 0);
    resolveInfo(info(6201));
    await pending;
    assert.equal(h.instances[0].url, 'ws://localhost:6201');
    assert.equal(h.instances[0].options.credentials.password, 'test-only');
    h.run('disconnectVNC()');
    const reconnect = h.run('connectVNC()');
    resolveInfo(info(6202));
    await reconnect;
    assert.equal(h.instances[1].url, 'ws://localhost:6202');
});

test('failed proxy or invalid port never falls back to another service', async () => {
    for (const result of [response({ running: false, error: 'occupied' }, false), info(0), info(65536)]) {
        const h = harness(async () => result);
        await h.run('connectVNC()');
        assert.equal(h.instances.length, 0);
        assert.equal(h.run('novncPort'), null);
    }
});

test('delayed screenshot and old disconnect cannot replace a new live connection', async () => {
    let resolveScreenshot;
    const h = harness(url => url === '/api/screenshot'
        ? new Promise(resolve => { resolveScreenshot = resolve; }) : Promise.resolve(info()));
    const screenshot = h.run('refreshScreenshot()');
    await h.run('connectVNC()');
    const old = h.instances[0];
    old.events.connect();
    resolveScreenshot(response({ success: true, screenshot: 'stale' }));
    await screenshot;
    assert.equal(h.element('screenshotFallback').style.display, 'none');
    h.run('disconnectVNC()');
    await h.run('connectVNC()');
    const latest = h.instances[1];
    latest.events.connect();
    old.events.disconnect({ detail: { clean: true } });
    assert.equal(h.run('rfb'), latest);
    assert.equal(h.element('vncCanvas').style.display, 'block');
});

test('blank password is omitted from both test and save requests', async () => {
    const calls = [];
    const h = harness(async (url, options) => {
        calls.push([url, options]);
        return url === '/api/novnc-info' ? info() : response({ success: true, resolution: '1280x800' });
    });
    h.element('vncHost').value = '127.0.0.1';
    h.element('vncPort').value = '5901';
    await h.run('saveSettings()');
    assert.deepEqual(calls.slice(0, 3).map(x => x[0]), [
        '/api/test-connection', '/api/vnc-config', '/api/novnc-info',
    ]);
    for (const [, options] of calls.slice(0, 2)) {
        assert.deepEqual(JSON.parse(options.body), { host: '127.0.0.1', port: 5901 });
    }
    assert.equal(h.alerts.length, 1);
    assert.match(h.alerts[0], /配置已保存/);
});

test('save failure is not reported as success and does not refresh credentials', async () => {
    const calls = [];
    const h = harness(async url => {
        calls.push(url);
        return url === '/api/test-connection' ? response({ success: true })
            : response({ success: false, error: 'save failed' }, false);
    });
    h.element('vncHost').value = '127.0.0.1';
    h.element('vncPort').value = '5901';
    await h.run('saveSettings()');
    assert.deepEqual(calls, ['/api/test-connection', '/api/vnc-config']);
    assert.match(h.alerts[0], /save failed/);
    assert.equal(h.element('#settingsModal .btn-primary').disabled, false);
});
