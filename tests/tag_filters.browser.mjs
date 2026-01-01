import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createInterface} from 'node:readline';
import fs from 'node:fs/promises';
import {chromium} from 'playwright';

const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
const server = spawn(python, ['tests/serve_tag_filters_fixture.py'], {stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true});
const output = createInterface({input: server.stdout});
const ready = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('Fixture startup timed out')), 60000);
  output.once('line', line => {clearTimeout(timer); resolve(JSON.parse(line));});
  server.once('error', reject);
  server.once('exit', code => {clearTimeout(timer); reject(new Error(`Fixture exited: ${code}`));});
});
let browser;
try {
  const {url: base} = await ready;
  browser = await chromium.launch({headless: true, channel: 'msedge'});
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}, reducedMotion: 'reduce', acceptDownloads: true});
  const errors = [], outside = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (!request.url().startsWith(base + '/')) outside.push(request.url());});
  const data = () => page.evaluate(() => window.CollectorMusicAnalytics.getAnalysisData());
  const wait = (filters = {}) => page.waitForFunction(expected => {
    const value = window.CollectorMusicAnalytics?.getAnalysisData();
    return value && !document.querySelector('#analytics').hasAttribute('aria-busy') &&
      Object.entries(expected).every(([key, val]) => value.filters[key] === val);
  }, filters);
  await page.goto(base, {waitUntil: 'networkidle'});
  await page.locator('#period').selectOption('7');
  await wait();
  assert.equal((await data()).summary.total, 6);
  assert.equal((await data()).catalog_count, 16);
  assert.equal(await page.locator('#catalog-body tr').count(), 8);
  assert.match(await page.locator('#catalog-body').textContent(), /平台未提供标签/);
  assert.match(await page.locator('#catalog-body').textContent(), /待补采/);
  assert.match(await page.locator('#catalog-body').textContent(), /采集失败/);
  await page.locator('#catalog-next').click();
  assert.equal(await page.locator('#catalog-page-info').textContent(), '2 / 2 页');
  // Keyboard access uses the same custom select as other dashboard controls.
  await page.locator('#select-filter-genre').click();
  await page.locator('#options-filter-genre [role="option"]').filter({hasText: '流行'}).focus();
  await page.keyboard.press('Enter');
  await wait({genre:'流行'});
  assert.equal((await data()).summary.total, 3);
  assert.equal((await data()).catalog_count, 2);
  await page.locator('#filter-language').selectOption('英语');
  await page.locator('#filter-tag').selectOption('活力');
  await wait({genre:'流行',language:'英语',tag:'活力'});
  assert.equal((await data()).summary.total, 2);
  assert.equal((await data()).comparison.total, 1);
  assert.equal(await page.locator('#catalog-body tr').count(), 1);
  await page.locator('[data-analytics-tab="rhythm"]').click();
  assert.equal(await page.locator('.heatmap-cell').evaluateAll(cells => cells.reduce((sum, cell) => sum + Number(cell.dataset.count), 0)), 2);
  await page.locator('[data-analytics-tab="artists"]').click();
  await page.locator('#artists-chart button').first().click();
  await page.waitForFunction(() => document.querySelector('#artist-dialog-tracks .analysis-track'));
  assert.match(await page.locator('#artist-dialog-note').textContent(), /2 条/);
  await page.keyboard.press('Escape');
  const downloading = page.waitForEvent('download');
  await page.locator('#analysis-export').click();
  const download = await downloading;
  const csv = await fs.readFile(await download.path(), 'utf8');
  assert.match(csv, /曲风筛选/);
  assert.match(csv, /"流行","英语","活力"/);
  await page.locator('#refresh').click();
  await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
  await wait({genre:'流行',language:'英语',tag:'活力'});
  assert.equal((await data()).summary.total, 2);

  // Search shares the filtered ID set and immediately invalidates old graphs.
  await page.locator('#filter-song-search').fill('124');
  await wait({q:'124'});
  assert.equal((await data()).catalog_count, 0);
  assert.equal((await data()).summary.total, 0);
  assert.equal(await page.locator('#catalog-empty').isVisible(), true);
  await page.locator('#tag-filter-reset').click();
  await wait({genre:'',language:'',tag:'',q:''});
  await page.locator('#filter-genre').selectOption('爵士');
  await wait({genre:'爵士'});
  assert.equal((await data()).catalog_count, 1);
  assert.equal((await data()).summary.total, 0);
  assert.match(await page.locator('#catalog-body').textContent(), /未记录/);
  await page.locator('[data-chart-source="pano"]').click();
  await page.waitForFunction(() => window.CollectorMusicAnalytics.getAnalysisData()?.source === 'pano');
  assert.equal((await data()).summary.total, 0);
  assert.match(await page.locator('#analysis-scope-note').textContent(), /没有可关联的网易云 ID/);
  await page.locator('[data-chart-source="netease_recent"]').click();
  await page.locator('#tag-filter-reset').click();
  await wait({genre:''});

  await page.route('**/api/analytics?**', route => route.fulfill({status:503,json:{error:'fixture offline'}}));
  await page.locator('#filter-tag').selectOption('放松');
  await page.waitForFunction(() => !document.querySelector('#analysis-retry').hidden);
  assert.equal(await page.locator('#catalog-body tr').count(), 0);
  assert.equal(await page.locator('#chart rect').count(), 0);
  await page.unroute('**/api/analytics?**');
  await page.locator('#analysis-retry').click();
  await wait({tag:'放松'});
  assert.equal((await data()).summary.total, 2);

  // Raw platform labels remain plain text, including in selectors and table cells.
  const raw = await (await fetch(base + '/api/dashboard')).json();
  raw.song_catalog.facets.genre.push({value:'<img src=x onerror="window.injected=true">',songs:1});
  await page.route('**/api/dashboard*', route => route.fulfill({json:raw}));
  await page.locator('#refresh').click();
  await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
  assert.equal(await page.locator('#options-filter-genre img').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  await page.unroute('**/api/dashboard*');
  await page.locator('[data-analytics-tab="trend"]').click();
  await fs.mkdir('.local/dashboard-qa', {recursive:true});
  for (const width of [1440, 1920]) {
    await page.setViewportSize({width,height:1100});
    await page.locator('#analytics').scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('#analytics').screenshot({path:`.local/dashboard-qa/tag-filters-${width}.png`});
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(outside, []);
  console.log('Desktop tag filters passed: ID dedupe, intersecting filters, distributions, unplayed songs, pending/empty/failed, pagination, keyboard, refresh, export, errors, XSS.');
} finally {
  await browser?.close();
  output.close();
  server.stdin.end();
  if (server.exitCode == null) await once(server,'exit');
}
