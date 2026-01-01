import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createInterface} from 'node:readline';
import fs from 'node:fs/promises';
import {chromium} from 'playwright';

const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
const server = spawn(python, ['tests/serve_reports_fixture.py'], {stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true});
const output = createInterface({input: server.stdout});
const ready = new Promise((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error('Report fixture did not start')), 60000);
  output.once('line', line => {clearTimeout(timeout); resolve(JSON.parse(line));});
  server.once('exit', code => {clearTimeout(timeout); reject(new Error(`Fixture server exited: ${code}`));});
  server.once('error', reject);
});
const monthly = {
  id: 'month-september', kind: 'monthly', status: 'ok', period_label: '2026-09',
  period_start: '2026-09-01', period_end: '2026-09-30', observed_at: '2026-10-30T00:00:00Z',
  metrics: {duration_minutes: 500, duration_raw: 500, duration_unit: 'minutes', listen_days: 10},
  daily: Array.from({length: 30}, (_, i) => ({date: `2026-09-${String(i + 1).padStart(2, '0')}`, duration_minutes: i % 4 === 0 ? 0 : i + 1})),
  genres: [{name: '流行', percent: 50}, {name: '电子', percent: 20}, {name: '摇滚', percent: 10}],
  languages: [{name: '华语', percent: 60}, {name: '英语', percent: 40}],
  ages: [{name: '2020年代', count: 40}, {name: '2010年代', count: 30}],
  top_song: {id: '100', name: 'A very long song title that wraps without widening the screen', artist: 'Example artist'},
  top_artist: {id: '200', name: 'Example artist'}
};
const realtime = {...structuredClone(monthly), id: 'realtime-october', kind: 'realtime', period_label: '2026-10',
  period_start: '2026-10-01', period_end: '2026-10-30', metrics: {duration_minutes: 300, listen_days: 6},
  daily: monthly.daily.map(row => ({...row, date: row.date.replace('09-', '10-')})),
  genres: [], languages: [], ages: [], top_song: null, top_artist: null};
let fixture = {status: 'ready', account_count: 1, monthly: [monthly], realtime,
  preference: {id: 'preferences', status: 'ok', observed_at: '2026-10-30T00:01:00Z', tags: [{name: '流行', percent: 50}]},
  total: null, issues: []};
let browser;
try {
  const {url: base} = await ready;
  const dashboard = await (await fetch(base + '/api/dashboard')).json();
  browser = await chromium.launch({headless: true, channel: 'msedge'});
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}, reducedMotion: 'reduce'});
  const errors = [], outside = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (!request.url().startsWith(base + '/')) outside.push(request.url());});
  await page.route('**/api/dashboard*', route => route.fulfill({json: {...dashboard, platform_reports: fixture}}));
  const refresh = async () => {
    await page.locator('#refresh').click();
    await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
  };
  await page.goto(base, {waitUntil: 'networkidle'});
  await page.locator('#platform-reports').scrollIntoViewIfNeeded();
  assert.equal(await page.locator('#report-period').inputValue(), monthly.id);
  assert.match(await page.locator('#reports-status').textContent(), /2026-09-01 — 2026-09-30/);
  assert.match(await page.locator('#report-metrics').textContent(), /500 分钟/);
  assert.equal(await page.locator('#report-daily button').count(), 30);
  assert.match(await page.locator('#report-breakdowns').textContent(), /50%/);
  await page.locator('#report-daily button').first().focus();
  await page.keyboard.press('ArrowRight');
  assert.match(await page.locator('#report-daily-detail').textContent(), /2026-09-02：2 分钟/);
  await page.locator('#wrap-report-period .custom-select-trigger').focus();
  await page.keyboard.press('Enter');
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#report-period').inputValue(), realtime.id);
  assert.match(await page.locator('#report-metrics').textContent(), /300 分钟/);
  assert.match(await page.locator('#report-source-note').textContent(), /实时快照，非完整月份/);
  assert.equal(await page.locator('#report-breakdowns .report-breakdown').count(), 0);
  await refresh();
  assert.equal(await page.locator('#report-period').inputValue(), realtime.id);
  fixture.realtime.id = 'realtime-october-new-capture';
  fixture.realtime.period_end = '2026-10-31';
  await refresh();
  assert.equal(await page.locator('#report-period').inputValue(), fixture.realtime.id);

  // Failure retains the selected period and explicitly marks its old data.
  fixture.realtime.status = 'stale';
  await refresh();
  assert.match(await page.locator('#reports-status').textContent(), /最近采集失败/);
  assert.match(await page.locator('#report-metrics').textContent(), /300 分钟/);
  fixture.realtime.status = 'empty';
  await refresh();
  assert.equal(await page.locator('#reports-content').isVisible(), false);
  assert.match(await page.locator('#reports-status').textContent(), /本次返回空报告/);
  fixture.realtime.status = 'stale';
  fixture.realtime.data_status = 'empty';
  await refresh();
  assert.equal(await page.locator('#reports-content').isVisible(), false);
  assert.match(await page.locator('#reports-status').textContent(), /上次成功采集为空报告/);

  // Units remain unknown instead of silently turning a raw scalar into minutes.
  fixture.realtime.status = 'ok';
  fixture.realtime.data_status = 'ok';
  fixture.realtime.metrics = {duration_raw: 300, duration_minutes: null, duration_unit: 'unknown', listen_days: 6};
  fixture.realtime.daily.forEach(row => {row.duration_raw = row.duration_minutes; row.duration_minutes = null;});
  await refresh();
  assert.equal(await page.locator('#report-daily-section').isVisible(), false);
  assert.doesNotMatch(await page.locator('#report-metrics').textContent(), /300/);
  assert.match(await page.locator('#report-source-note').textContent(), /时长单位待核实/);

  await page.locator('#report-period').selectOption(monthly.id);
  await page.locator('#report-preference summary').click();
  assert.match(await page.locator('#report-preference-date').textContent(), /2026\/10\/30/);
  await fs.mkdir('.local/dashboard-qa', {recursive: true});
  await page.locator('#platform-reports').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'desktop overflow');
  await page.locator('.masthead').evaluate(el => { el.style.visibility = 'hidden'; });
  await page.locator('#platform-reports').screenshot({path: '.local/dashboard-qa/reports-1440.png'});
  await page.locator('.masthead').evaluate(el => { el.style.visibility = ''; });
  fixture.monthly[0].genres[0].name = '<img src=x onerror="window.injected=true">';
  fixture.monthly[0].top_song.name = '<script>window.injected=true</script>';
  await refresh();
  assert.equal(await page.locator('#platform-reports img, #platform-reports script').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  assert.match(await page.locator('#report-breakdowns').textContent(), /<img src=x/);

  fixture = {status: 'unavailable', monthly: [], realtime: null, preference: null, issues: ['failed']};
  await refresh();
  assert.match(await page.locator('#reports-status').textContent(), /采集未完成/);
  assert.equal(await page.locator('#reports-content').isVisible(), false);
  assert.equal(await page.locator('#report-period').isDisabled(), true);
  fixture = null; // An older publication has no platform report dataset.
  await refresh();
  assert.match(await page.locator('#reports-status').textContent(), /尚未采集/);
  assert.deepEqual(errors, []);
  assert.deepEqual(outside, []);
  console.log('Platform reports browser checks passed: actual periods, realtime separation, selection retention, keyboard chart/selector, empty/stale/unknown units, XSS, no external requests, desktop layout.');
} finally {
  await browser?.close();
  output.close();
  server.stdin.end();
  if (server.exitCode == null) await once(server, 'exit');
}
