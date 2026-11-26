// Opens the live page like a phone would and reports whether it follows the bot every second.
// node tools/pagecheck.js [url] [seconds]
const { chromium } = require('playwright');
(async () => {
  const url = (process.argv[2] || 'https://karpens.github.io/memebot/') + '?t=' + Date.now();
  const secs = +(process.argv[3] || 30);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 400, height: 860 }, deviceScaleFactor: 2 });
  const errs = [], fails = [], ws = [];
  p.on('pageerror', e => errs.push(String(e)));
  p.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
  p.on('requestfailed', r => fails.push(r.url().slice(0, 120) + ' ' + (r.failure() || {}).errorText));
  p.on('websocket', w => { ws.push(w.url()); w.on('close', () => ws.push('closed ' + w.url())); });
  await p.goto(url, { waitUntil: 'load' });
  await p.waitForTimeout(1500);
  await p.click('#run').catch(() => {});
  const seen = [];
  for (let i = 0; i < secs; i++) {
    await p.waitForTimeout(1000);
    const s = await p.evaluate(() => {
      const pill = [...document.querySelectorAll('*')].find(e => /^(LIVE|OFFLINE|SNAPSHOT)/.test((e.textContent || '').trim()) && e.children.length < 3);
      const hero = document.body.innerText.match(/\$[0-9.,]+\s*balance/);
      return { pill: pill ? pill.textContent.trim().slice(0, 40) : null, bal: hero ? hero[0] : null, mqtt: typeof window.mqtt };
    });
    seen.push(s);
  }
  await p.screenshot({ path: 'pagecheck.png' });
  const pills = seen.map(s => s.pill);
  console.log('pills:', JSON.stringify([...new Set(pills)]));
  console.log('balances:', JSON.stringify([...new Set(seen.map(s => s.bal))]));
  console.log('mqtt lib:', seen[seen.length - 1].mqtt);
  console.log('websockets:', JSON.stringify(ws));
  console.log('failed requests:', JSON.stringify(fails.slice(0, 10)));
  console.log('errors:', JSON.stringify(errs.slice(0, 10)));
  console.log(pills.filter(x => x && x.startsWith('LIVE · 1s')).length >= secs / 2 ? 'RESULT: LIVE every second' : 'RESULT: NOT live every second');
  await b.close();
})();
