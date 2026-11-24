// Exercise the generated bundle in a browser, including its own fetch and WebSocket.
const assert = require('node:assert/strict');
const { chromium } = require('playwright-core');

async function probe(origin) {
  const browser = await chromium.launch({ executablePath: process.env.SYNEX_SMOKE_CHROMIUM });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(15000);
    const expectedSocket = origin.replace(/^http/, 'ws') + '/ws';
    const hello = page.waitForEvent('websocket', ws => ws.url() === expectedSocket)
      .then(ws => ws.waitForEvent('framereceived', frame => JSON.parse(frame.payload).type === 'hello'));
    const [alerts] = await Promise.all([
      page.waitForResponse(r => r.url() === origin + '/api/alerts?status=active'),
      hello,
      page.goto(origin),
    ]);
    assert.equal(alerts.status(), 200);
    assert.ok(Array.isArray((await alerts.json()).alerts));
    const health = await page.evaluate(async () => {
      const response = await fetch('/api/health');
      if (!response.ok) throw new Error('Health check: HTTP ' + response.status);
      return response.json();
    });
    assert.equal(health.status, 'ok');
    assert.equal(health.worker.connected, true);
    console.log('Browser OK:', origin, '/api/health, /api/alerts and /ws hello');
  } finally {
    await browser.close();
  }
}

probe(process.argv[2]).catch(error => { console.error(error); process.exitCode = 1; });
