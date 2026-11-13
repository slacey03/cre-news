// MOCKUP ONLY: render dashboard.html + map add-on with stubbed data and screenshot it.
const { chromium, devices } = require('playwright'); const fs = require('fs');
const B = 'mockup-map/build', OUT = 'mockup-map/out';
const P = JSON.parse(fs.readFileSync(B + '/pipeline.json')), T = JSON.parse(fs.readFileSync(B + '/teaser.json')), MP = JSON.parse(fs.readFileSync(B + '/pulse.json'));
const stub = member => `window.supabase={createClient(){const P=${JSON.stringify(P)};const T=${JSON.stringify(T)};const MP=${JSON.stringify(MP)};
 const q=t=>{const o={select(){return o},order(){return o},limit(){return o},
 single(){return Promise.resolve({data:{projects:P.length,municipalities:41,documents:427,active_30d:211}})},
 then(r){return Promise.resolve({data:t==='pipeline'?(${member}?P:[]):t==='market_pulse'?MP:T}).then(r)}};return o;};
 return {from:q,rpc:()=>Promise.resolve({data:false}),auth:{getSession:()=>Promise.resolve({data:{session:${member}?{user:{email:'member@example.com'}}:null}}),onAuthStateChange(){},signOut(){}}};}};`;
(async () => {
  const b = await chromium.launch(); const errs = [];
  async function page(dev, member) {
    const c = await b.newContext(dev); const pg = await c.newPage();
    pg.on('pageerror', e => errs.push(e.message));
    await pg.route('**/supabase.min.js', r => r.fulfill({ contentType: 'application/javascript', body: stub(member) }));
    await pg.goto('http://127.0.0.1:8765/dashboard.html'); await pg.waitForTimeout(2500);
    return [c, pg];
  }
  let pick = null;
  async function choose(pg) { // a pinned notable commercial project (with news if possible), else any pin
    return pg.evaluate(() => {
      const ids = Object.keys(window.__markers || {}); const want = ['retail', 'medical', 'mixed-use', 'restaurant', 'office'];
      const rows = ROWS.filter(r => ids.includes(r.id));
      const r = rows.find(r => want.includes(r.category) && (r.news || []).length) || rows.find(r => want.includes(r.category)) || rows[0];
      return r && r.id;
    });
  }
  const tiles = pg => pg.waitForFunction(() => [...document.querySelectorAll('.leaflet-tile')].every(t => t.complete), null, { timeout: 15000 }).catch(() => {});
  const desk = { viewport: { width: 1400, height: 1500 } };
  let [c, pg] = await page(desk, true);
  pick = await choose(pg); console.log('markers', await pg.evaluate(() => Object.keys(window.__markers || {}).length), 'pick', pick);
  await pg.evaluate(id => window.__markers[id].fire('click'), pick); await pg.waitForTimeout(800); await tiles(pg);
  const top = await pg.evaluate(() => document.querySelector('.map-panel').offsetTop - 140);
  await pg.evaluate(y => window.scrollTo(0, y), top); await pg.waitForTimeout(500);
  await pg.screenshot({ path: OUT + '/map-desktop-selected.png' }); await c.close();
  [c, pg] = await page(desk, true);
  await pg.click('.filter-btn[data-filter=industrial]'); await pg.waitForTimeout(800); await tiles(pg);
  await pg.evaluate(() => window.scrollTo(0, document.querySelector('.map-panel').offsetTop - 140)); await pg.waitForTimeout(400);
  await pg.screenshot({ path: OUT + '/map-desktop-industrial.png', clip: { x: 0, y: 0, width: 1400, height: 820 } }); await c.close();
  [c, pg] = await page({ viewport: { width: 1400, height: 1200 } }, false); await tiles(pg);
  await pg.evaluate(() => window.scrollTo(0, document.querySelector('.map-panel').offsetTop - 140)); await pg.waitForTimeout(400);
  await pg.screenshot({ path: OUT + '/map-desktop-public-teaser.png' }); await c.close();
  const ph = devices['iPhone 13'];
  [c, pg] = await page(ph, true);
  await pg.evaluate(() => window.scrollTo(0, document.querySelector('.map-panel').offsetTop - 10)); await pg.waitForTimeout(300);
  await pg.screenshot({ path: OUT + '/map-phone-1-list.png' });
  await pg.click('.seg button[data-v=map]'); await pg.waitForTimeout(800); await tiles(pg);
  await pg.screenshot({ path: OUT + '/map-phone-2-map.png' });
  await pg.evaluate(id => { const m = window.__markers[id]; window.__map.setView(m.getLatLng(), 14); m.openPopup(); }, pick);
  await pg.waitForTimeout(1200); await tiles(pg);
  await pg.screenshot({ path: OUT + '/map-phone-3-pin.png' });
  await pg.click('.leaflet-popup a'); await pg.waitForTimeout(800);
  await pg.screenshot({ path: OUT + '/map-phone-4-project.png' }); await c.close();
  fs.appendFileSync(OUT + '/stats.txt', 'page errors: ' + JSON.stringify(errs) + '\n');
  console.log('errors', errs); await b.close();
})();
