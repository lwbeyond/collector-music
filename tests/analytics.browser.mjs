import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createInterface} from 'node:readline';
import fs from 'node:fs/promises';
import {chromium} from 'playwright';

const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
const desktopOnly = process.argv.includes('--desktop-only');
const server = spawn(python, ['tests/serve_analytics_fixture.py'], {stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true});
const output = createInterface({input: server.stdout});
const ready = new Promise((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error('Fixture server did not start')), 180000);
  output.once('line', line => {clearTimeout(timeout); resolve(JSON.parse(line));});
  server.once('exit', code => {clearTimeout(timeout); reject(new Error(`Fixture server exited: ${code}`));});
  server.once('error', reject);
});
let browser;
try {
  const {url: base, today} = await ready;
  const day = offset => new Date(Date.parse(today) + offset * 86400000).toISOString().slice(0, 10);
  browser = await chromium.launch({headless: true, channel: 'msedge'});
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}, acceptDownloads: true, reducedMotion: 'reduce'});
  const errors = [], outside = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', r => {if (!r.url().startsWith(base + '/')) outside.push(r.url());});
  const wait = () => page.waitForFunction(() => window.CollectorMusicAnalytics?.getAnalysisData() && !document.querySelector('#analytics').hasAttribute('aria-busy'));
  const data = () => page.evaluate(() => window.CollectorMusicAnalytics.getAnalysisData());
  await page.goto(base, {waitUntil: 'networkidle'});
  await wait();
  assert.equal((await data()).summary.total, 1);
  assert.equal(await page.evaluate(() => window.CollectorMusicAnalytics.getState().data.records_truncated), true);
  await page.locator('[data-chart-source="pano"]').click();
  await page.locator('#period').selectOption('all');
  await wait();
  assert.equal((await data()).summary.total, 5006);
  assert.equal((await data()).summary.unique_tracks, 4);
  await page.locator('[data-analytics-tab="artists"]').click();
  await page.getByRole('button', {name: /Old only/}).click();
  await page.waitForFunction(() => document.querySelector('#artist-dialog-tracks .analysis-track'));
  assert.match(await page.locator('#artist-dialog-tracks').textContent(), /Old song/);
  assert.equal(await page.evaluate(() => window.CollectorMusicAnalytics.getState().data.records.some(r => r.artist === 'Old only')), false);
  await page.keyboard.press('Escape');
  await page.locator('#period').selectOption('7');
  await wait();
  assert.equal((await data()).summary.total, 5003);
  assert.equal((await data()).comparison.total, 2);
  await page.locator('[data-analytics-tab="rhythm"]').click();
  assert.equal(await page.locator('.heatmap-cell').count(), 168);
  assert.equal(await page.locator('.heatmap-cell').evaluateAll(cells => cells.reduce((sum, c) => sum + Number(c.dataset.count), 0)), 5003);
  const firstCell = page.locator('.heatmap-cell').first();
  await firstCell.focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('.heatmap-cell').nth(1).evaluate(e => e === document.activeElement), true);
  await page.keyboard.press('Enter');
  assert.match(await page.locator('#rhythm-cell-detail').textContent(), /01:00/);
  await page.locator('#tab-rhythm').focus();
  await page.keyboard.press('End');
  assert.equal(await page.locator('#view-matrix').isVisible(), true);
  assert.equal(await page.locator('#matrix-daily-avg').textContent(), '2501.5');
  assert.equal(await page.locator('#matrix-streak').textContent(), '2 天');
  assert.equal(await page.locator('#matrix-peak-day').textContent(), '5,001');
  assert.equal(await page.locator('#matrix-diversity').textContent(), '<0.1%');

  // Inclusive custom dates, empty state, invalid range, keyboard selection, export.
  await page.locator('#period').selectOption('custom');
  await page.locator('#analysis-start').fill(day(-40));
  await page.locator('#analysis-end').fill(day(-40));
  await page.locator('#analysis-end').press('Tab');
  await wait();
  assert.equal((await data()).summary.total, 1);
  const downloading = page.waitForEvent('download');
  await page.locator('#analysis-export').click();
  const download = await downloading;
  const content = await fs.readFile(await download.path(), 'utf8');
  assert.match(download.suggestedFilename(), /music-daily-pano/);
  assert.ok(content.includes(`"${day(-40)}","pano","1","Asia/Shanghai"`));
  assert.ok(content.includes('observed_only'));
  await page.locator('#analysis-start').fill(day(-39));
  await page.locator('#analysis-start').press('Tab');
  await page.waitForFunction(() => document.querySelector('#analysis-status').textContent.includes('开始不晚于结束'));
  assert.equal(await page.locator('#analysis-export').isDisabled(), true);
  assert.equal(await page.locator('#chart rect').count(), 0);
  await page.locator('#analysis-end').fill(day(-39));
  await page.locator('#analysis-end').press('Tab');
  await wait();
  assert.equal((await data()).summary.total, 0);
  assert.equal(await page.locator('#matrix-diversity').textContent(), '—');
  assert.match(await page.locator('#analysis-status').textContent(), /没有可分析记录/);
  await page.locator('#wrap-period .custom-select-trigger').focus();
  await page.keyboard.press('Enter');
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  await wait();
  assert.equal(await page.locator('#period').inputValue(), '7');

  // A slow obsolete response cannot overwrite the latest selection.
  let release, reached;
  const gate = new Promise(r => { release = r; });
  const arrived = new Promise(r => { reached = r; });
  await page.route('**/api/analytics?*', async route => {
    const response = await route.fetch();
    if (new URL(route.request().url()).searchParams.get('period') === '90') {
      reached(); await gate;
    }
    await route.fulfill({response}).catch(() => {});
  });
  await page.locator('#period').selectOption('90');
  await arrived;
  await page.locator('[data-chart-source="netease_recent"]').click();
  await page.locator('#period').selectOption('7');
  await wait();
  release();
  await page.unrouteAll({behavior: 'wait'});
  assert.equal((await data()).source, 'netease_recent');
  assert.equal((await data()).summary.total, 1);

  // HTTP failures clear stale charts and expose a working retry.
  await page.route('**/api/analytics?*', route => route.fulfill({status: 503, json: {error: '分析暂不可用（测试）'}}));
  await page.locator('#period').selectOption('30');
  await page.locator('#analysis-retry').waitFor();
  assert.equal(await page.locator('#chart rect').count(), 0);
  assert.equal(await page.locator('#analysis-export').isDisabled(), true);
  assert.match(await page.locator('#analysis-status').textContent(), /暂不可用/);
  await page.unrouteAll();
  await page.locator('#analysis-retry').click();
  await wait();

  // Source text stays inert in charts, insight text and drilldowns.
  const malicious = '<img src=x onerror="window.injected=true">';
  await page.route('**/api/analytics?*', async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.artists[0].artist = malicious;
    body.tracks[0].track = malicious;
    body.tracks[0].platform_track_id = 'javascript:alert(1)';
    await route.fulfill({json: body});
  });
  await page.locator('#period').selectOption('all');
  await wait();
  await page.locator('#tab-artists').click();
  assert.equal(await page.locator('#analytics img').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  assert.ok((await page.locator('#analysis-tracks a').first().getAttribute('href')).startsWith('https://music.163.com/#/search/'));
  await page.unrouteAll();
  await page.locator('#period').selectOption('7');
  await wait();

  await fs.mkdir('.local/dashboard-qa', {recursive: true});
  await page.locator('[data-chart-source="pano"]').click();
  await wait();
  for (const width of desktopOnly ? [1440] : [1440, 390]) {
    await page.setViewportSize({width, height: 1600});
    for (const tab of ['trend', 'rhythm', 'artists', 'matrix']) {
      await page.locator('#tab-' + tab).click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.locator('#analytics').screenshot({path: `.local/dashboard-qa/analytics-${tab}-${width}.png`});
    }
  }
  await page.route('**/api/dashboard*', route => route.fulfill({json: {status: 'empty', message: '无发布（测试）'}}));
  await page.locator('#refresh').click();
  await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
  for (const selector of ['#chart rect', '.heatmap-cell', '#analysis-insights li', '#analysis-tracks .analysis-track']) assert.equal(await page.locator(selector).count(), 0);
  assert.equal(await page.locator('#matrix-streak').textContent(), '—');
  assert.deepEqual(errors, []);
  assert.deepEqual(outside, []);
  console.log(`Analytics browser checks passed: full history, drilldown, ranges, timezone totals, keyboard, CSV, races, failure/retry, XSS, ${desktopOnly ? 'desktop' : 'desktop/mobile'}, empty state.`);
} finally {
  await browser?.close();
  server.stdin.end();
  if (server.exitCode === null) await once(server, 'exit');
  output.close();
}
