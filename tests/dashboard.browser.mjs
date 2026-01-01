// Start the local dashboard first. Screenshots and all real data stay under .local.
import assert from 'node:assert/strict';
const desktopOnly = process.argv.includes('--desktop-only');
import { chromium } from 'playwright';
import fs from 'node:fs/promises';

const base = process.env.MUSIC_DASHBOARD_URL || 'http://127.0.0.1:8767';

// Keep paging and date-boundary coverage independent of the private archive size.
async function checkUsability(page, data) {
  const fixture = structuredClone(data);
  const day = offset => new Date(Date.parse(data.today + 'T12:00:00Z') - offset * 86400000).toISOString().slice(0, 10);
  fixture.timezone = 'Asia/Shanghai';
  fixture.records = Array.from({length: 240}, (_, index) => ({
    ...data.records[0], id: `ux-record-${index}`, track: `UX song ${String(index).padStart(3, '0')}`,
    artist: 'UX artist', album: 'UX album', source: index % 2 ? 'netease_recent' : 'pano',
    event_kind: index % 2 ? 'recent_observation' : 'scrobble', platform_track_id: String(990000 + index),
    occurred_at_utc: `${day(Math.floor(index / 2))}T${index % 2 ? '23:55' : '00:05'}:00+08:00`
  })).reverse();
  fixture.records.push(
    {...fixture.records[0], id: 'ux-future', track: 'UX future ' + 'LongSongWithoutBreak'.repeat(12), occurred_at_utc: day(-1) + 'T12:00:00+08:00'},
    {...fixture.records[1], id: 'ux-unknown', track: 'UX unknown time', occurred_at_utc: 'invalid-time'}
  );
  fixture.overlap_candidates = [];
  fixture.records_truncated = false;
  fixture.likes = Array.from({length: 35}, (_, index) => ({
    ...data.likes[0], track_id: String(980000 + index), track: `UX like ${index}`, artist: 'UX artist', album: 'UX album'
  }));
  fixture.playlists = Array.from({length: 35}, (_, index) => ({
    ...data.playlists[0], entity_id: String(970000 + index), label: `UX playlist ${index}`, kind: index % 2 ? 'subscribed' : 'created'
  }));
  fixture.stats = {...fixture.stats, pano: 121, recent: 121, likes: 35, playlists: 35};
  let refreshGate = null;
  const dashboardRoute = async route => {
    if (refreshGate && new URL(route.request().url()).searchParams.has('refresh')) await refreshGate;
    await route.fulfill({json: fixture});
  };
  const jump = async (input, form, value) => {
    await page.locator(input).fill(String(value));
    await page.locator(`${form} button[type="submit"]`).click();
    assert.equal(await page.locator(input).inputValue(), String(value));
  };
  const rowTitles = () => page.locator('#records-body .song-link').allTextContents();
  const recordTotal = async () => Number((await page.locator('#record-count').textContent()).match(/[\d,]+/)[0].replaceAll(',', ''));
  const viewState = () => page.evaluate(() => Object.fromEntries(
    ['search', 'record-source', 'record-period', 'record-sort', 'record-size', 'record-page', 'collection-search', 'collection-kind', 'collection-page']
      .map(id => [id, document.getElementById(id).value])
      .concat(['page-info', 'collection-page-info'].map(id => [id, document.getElementById(id).textContent]))
  ));

  await page.route('**/api/dashboard*', dashboardRoute);
  try {
    await page.goto(base, {waitUntil: 'networkidle'});
    await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
    assert.equal(await page.locator('#search-history').getAttribute('href'), '#records');
    await page.locator('#search-history').click();
    assert.equal(new URL(page.url()).hash, '#records');

    // The visible trigger owns the accessible name and keyboard interaction.
    for (const select of await page.locator('select').all()) {
      const id = await select.getAttribute('id');
      const label = (await page.locator(`label[for="select-${id}"]`).first().textContent()).trim();
      assert.ok(label, `${id} needs a field label`);
      assert.equal(await select.getAttribute('tabindex'), '-1');
      assert.equal(await select.getAttribute('aria-hidden'), 'true');
      const trigger = page.locator(`#wrap-${id} .custom-select-trigger`);
      assert.ok((await trigger.getAttribute('aria-label')).includes(label), `${id} trigger must name its field`);
      const controlled = await trigger.getAttribute('aria-controls');
      assert.ok(controlled);
      assert.equal(await page.locator(`#${controlled}`).getAttribute('role'), 'listbox');
    }
    const sourceTrigger = page.locator('#wrap-record-source .custom-select-trigger');
    const sourceMenu = page.locator('#wrap-record-source .custom-select-dropdown');
    await sourceTrigger.click();
    assert.equal(await sourceMenu.isVisible(), true);
    assert.equal(await sourceTrigger.getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#record-source').evaluate(el => el === document.activeElement), false);
    assert.equal(await sourceMenu.evaluate(el => el.contains(document.activeElement)), true);
    await page.keyboard.press('Escape');
    await sourceTrigger.focus();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Home');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), 'all');
    await page.keyboard.press('End');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), 'netease_recent');
    await page.keyboard.press('ArrowUp');
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#record-source').inputValue(), 'pano');
    assert.equal(await sourceMenu.isVisible(), false);
    assert.equal(await sourceTrigger.evaluate(el => el === document.activeElement), true);
    await sourceTrigger.click();
    await page.keyboard.press('Tab');
    await page.waitForFunction(() => document.querySelector('#wrap-record-source .custom-select-dropdown').hidden);
    assert.equal(await sourceTrigger.getAttribute('aria-expanded'), 'false');

    // A shortcut must recover from conflicting searches and date filters.
    for (const source of ['pano', 'netease_recent']) {
      await page.locator('#search').fill('no-such-ux-song');
      await page.locator('#record-period').selectOption('7');
      await page.locator(`.stats [data-record-shortcut="${source}"]`).click();
      assert.equal(new URL(page.url()).hash, '#records');
      assert.equal(await page.locator('#search').inputValue(), '');
      assert.equal(await page.locator('#record-source').inputValue(), source);
      assert.equal(await page.locator('#record-period').inputValue(), 'all');
      assert.equal(await recordTotal(), 121);
    }
    await page.locator('#record-source').selectOption('all');
    for (const period of ['7', '30', '90']) {
      await page.locator('#record-period').selectOption(period);
      assert.equal(await recordTotal(), Number(period) * 2, 'Include today in the local timezone; exclude future/invalid timestamps');
      assert.equal(await page.locator('#record-page').inputValue(), '1');
    }
    await page.locator('#record-period').selectOption('7');
    await page.locator('#record-size').selectOption('25');
    assert.ok((await rowTitles()).includes('UX song 012'), 'Local midnight on the first included day must not fall into the previous UTC date');
    await page.locator('#record-period').selectOption('all');
    assert.equal(await recordTotal(), 242);
    await page.locator('#record-sort').selectOption('oldest');
    assert.equal((await rowTitles())[0], 'UX song 238');
    await page.locator('#record-sort').selectOption('newest');
    assert.ok((await rowTitles())[0].startsWith('UX future '));
    for (const size of ['10', '25', '50']) {
      await page.locator('#record-size').selectOption(size);
      assert.equal(await page.locator('#records-body tr').count(), Number(size));
      const lastPage = Math.ceil(fixture.records.length / Number(size));
      assert.equal(await page.locator('#record-page').getAttribute('max'), String(lastPage));
      await jump('#record-page', '#record-page-form', lastPage);
      assert.equal((await rowTitles()).at(-1), 'UX unknown time');
      assert.equal(await page.locator('#next').isDisabled(), true);
    }
    await page.locator('#record-sort').selectOption('oldest');
    await jump('#record-page', '#record-page-form', 5);
    assert.equal((await rowTitles()).at(-1), 'UX unknown time', 'Unknown times remain last in either direction');
    await page.locator('#search').fill('no-such-ux-song');
    assert.equal(await page.locator('#records-empty').isVisible(), true);
    await page.locator('#record-reset').click();
    assert.equal(await page.locator('#search').inputValue(), '');
    assert.equal(await page.locator('#record-source').inputValue(), 'netease_recent');
    assert.equal(await page.locator('#record-period').inputValue(), 'all');
    assert.equal(await page.locator('#record-sort').inputValue(), 'newest');
    assert.equal(await page.locator('#record-size').inputValue(), '50');
    assert.equal(await page.locator('#record-page').inputValue(), '1');

    for (const collection of ['likes', 'playlists']) {
      await page.locator('[data-collection="playlists"]').click();
      await page.locator('#collection-search').fill('no-such-ux-collection');
      await page.locator('#collection-kind').selectOption('subscribed');
      await page.locator(`.stats [data-collection-shortcut="${collection}"]`).click();
      assert.equal(new URL(page.url()).hash, '#collection-panel');
      assert.equal(await page.locator(`[data-collection="${collection}"]`).getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('#collection-search').inputValue(), '');
      assert.equal(await page.locator('#collection-kind').inputValue(), 'all');
      await jump('#collection-page', '#collection-page-form', 4);
      assert.equal(await page.locator(collection === 'likes' ? '.favorite-row' : '.playlist-card').count(), 5);
      assert.equal(await page.locator('#collection-next').isDisabled(), true);
    }
    await page.locator('#collection-search').fill('no-such-ux-collection');
    await page.locator('#collection-kind').selectOption('subscribed');
    await page.locator('#collection-reset').click();
    assert.equal(await page.locator('#collection-search').inputValue(), '');
    assert.equal(await page.locator('#collection-kind').inputValue(), 'all');
    assert.equal(await page.locator('#collection-page').inputValue(), '1');

    await page.locator('#record-source').selectOption('all');
    await page.locator('#record-period').selectOption('30');
    await page.locator('#record-sort').selectOption('oldest');
    await page.locator('#record-size').selectOption('25');
    await page.locator('#search').fill('UX song');
    await jump('#record-page', '#record-page-form', 2);
    await page.locator('#collection-search').fill('UX playlist');
    await page.locator('#collection-kind').selectOption('subscribed');
    await jump('#collection-page', '#collection-page-form', 2);
    const beforeRefresh = await viewState();
    const beforeRows = await rowTitles();
    const refreshLabel = await page.locator('#refresh-label').textContent();
    let releaseRefresh;
    refreshGate = new Promise(resolve => {releaseRefresh = resolve;});
    try {
      await page.locator('#refresh').click();
      assert.equal(await page.locator('#refresh').isDisabled(), true);
      assert.equal(await page.locator('#refresh').getAttribute('aria-busy'), 'true');
      assert.notEqual(await page.locator('#refresh-label').textContent(), refreshLabel);
    } finally {
      releaseRefresh();
    }
    await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
    assert.deepEqual(await viewState(), beforeRefresh);
    assert.deepEqual(await rowTitles(), beforeRows);
    assert.equal(await page.locator('#refresh-label').textContent(), '已是最新展示');

    await page.locator('#record-reset').click();
    await page.locator('#record-size').selectOption('10');
    for (const width of desktopOnly ? [] : [390, 320]) {
      await page.setViewportSize({width, height: 844});
      await page.locator('#search-history').click();
      await page.waitForFunction(() => Math.abs(document.querySelector('.masthead').getBoundingClientRect().top) < 2);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.equal(await page.locator('.table-scroll').evaluate(el => el.scrollWidth > el.clientWidth), false);
      assert.notEqual(await page.locator('#records-body tr').first().evaluate(el => getComputedStyle(el).display), 'table-row');
      await page.locator('#wrap-record-period .custom-select-trigger').click();
      const menu = await page.locator('#wrap-record-period .custom-select-dropdown').boundingBox();
      assert.ok(menu.x >= 0 && menu.x + menu.width <= width + 1, 'Mobile filter menu fits the viewport');
      await page.keyboard.press('Escape');
    }
  } finally {
    await page.unroute('**/api/dashboard*', dashboardRoute);
    await page.setViewportSize({width: 1440, height: 1100});
    await page.goto(base, {waitUntil: 'networkidle'});
  }
}

const browser = await chromium.launch({headless: true, channel: 'msedge'});
try {
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
  const waitAnalysis = () => page.waitForFunction(() => window.CollectorMusicAnalytics?.getAnalysisData() && !document.querySelector('#analytics').hasAttribute('aria-busy'));
  const errors = [], outsideRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {if (message.type() === 'error') errors.push(message.text());});
  page.on('request', request => {if (!request.url().startsWith(base + '/')) outsideRequests.push(request.url());});
  const response = page.waitForResponse(response => response.url() === base + '/api/dashboard');
  await page.goto(base, {waitUntil: 'networkidle'});
  const data = await (await response).json();
  assert.equal(data.status, 'ready');
  await page.waitForFunction(() => document.querySelector('#stat-pano').textContent !== '—');
  assert.equal(await page.locator('#stat-pano').textContent(), data.stats.pano.toLocaleString('zh-CN'));
  assert.equal(await page.locator('#stat-recent').textContent(), data.stats.recent.toLocaleString('zh-CN'));
  assert.equal(await page.locator('#record-source').inputValue(), 'netease_recent');
  assert.ok((await page.locator('#record-scope-note').textContent()).includes('不代表完整播放流水'));
  await fs.mkdir('.local/dashboard-qa', {recursive: true});
  await page.screenshot({path: '.local/dashboard-qa/desktop.png', fullPage: true});
  await page.screenshot({path: '.local/dashboard-qa/preview.png'});
  await page.locator('#search').fill('nonexistent-query-3891057');
  assert.equal(await page.locator('#records-empty').isVisible(), true);
  await page.locator('#search').fill('');

  // 验证定制 Hi-Fi 下拉框 UI 与交互行为
  assert.equal(await page.locator('#wrap-record-source').isVisible(), true);
  await page.locator('#wrap-record-source .custom-select-trigger').click();
  assert.equal(await page.locator('#wrap-record-source .custom-select-dropdown').isVisible(), true);
  assert.equal(await page.locator('#wrap-record-source .custom-select-trigger').getAttribute('aria-expanded'), 'true');
  await page.locator('#wrap-record-source [data-value="pano"]').click();
  assert.equal(await page.locator('#wrap-record-source .custom-select-dropdown').isVisible(), false);
  assert.equal(await page.locator('#record-source').inputValue(), 'pano');
  assert.equal(await page.locator('#wrap-record-source .custom-select-label').textContent(), '设备记录');
  const pano = data.records.filter(r => r.source === 'pano');
  assert.equal(await page.locator('#records-body tr').count(), Math.min(10, pano.length));

  // 测试 Escape 键收起自定义下拉菜单
  await page.locator('#wrap-record-source .custom-select-trigger').click();
  assert.equal(await page.locator('#wrap-record-source .custom-select-dropdown').isVisible(), true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#wrap-record-source .custom-select-dropdown').isVisible(), false);

  await page.locator('#record-source').selectOption('all');
  assert.equal(await page.locator('#wrap-record-source .custom-select-label').textContent(), '全部来源（核对）');
  assert.ok((await page.locator('#record-scope-note').textContent()).includes('未作去重'));
  for (const link of data.overlap_candidates || []) {
    const row = data.records.find(r=>r.id===link.left_id);
    await page.locator('#search').fill(row.track);
    assert.ok(await page.locator('[data-overlap]').count() >= 2);
  }
  await page.locator('#search').fill('');
  if (data.records.length > 10) {
    await page.locator('#next').click();
    assert.match(await page.locator('#page-info').textContent(), /^11/);
  }
  await page.locator('[data-chart-source="pano"]').click();
  await page.locator('#period').selectOption('all');
  await waitAnalysis();
  const panoDaily = data.daily.filter(d => d.source === 'pano' && d.date <= data.today).reduce((a,b) => a + b.records, 0);
  assert.equal(await page.locator('#chart-total').textContent(), panoDaily.toLocaleString('zh-CN'));
  await page.locator('#ranking-scope').selectOption('all');
  if (data.rankings.all.length > 8) {
    await page.locator('#ranking-more').click();
    assert.equal(await page.locator('#ranking-list li').count(), Math.min(18, data.rankings.all.length));
  }

  // 验证数据指标分析前台框架及多维视图
  await page.locator('[data-analytics-tab="rhythm"]').click();
  assert.equal(await page.locator('#view-rhythm').isVisible(), true);
  assert.equal(await page.locator('#rhythm-bars .rhythm-col').count(), 24);
  assert.equal(await page.locator('[data-chart-source="pano"]').isVisible(), true);
  const analyzedPano = await page.evaluate(()=>window.CollectorMusicAnalytics.getAnalysisRecords());
  assert.ok(analyzedPano.every(r=>r.source==='pano' && r.event_kind==='scrobble'));
  const hourTotal = (await page.locator('.rhythm-pillar').evaluateAll(items=>items.map(e=>Number(e.dataset.count)))).reduce((a,b)=>a+b,0);
  assert.equal(hourTotal, panoDaily);
  assert.equal(await page.locator('.heatmap-cell').count(), 168);

  await page.locator('[data-analytics-tab="artists"]').click();
  assert.equal(await page.locator('#view-artists').isVisible(), true);
  if (await page.locator('.artist-bar-item').count() > 0) {
    const topArtistName = await page.locator('.artist-bar-item .artist-name').first().textContent();
    await page.locator('.artist-bar-item').first().click();
    await page.waitForFunction(() => !document.querySelector('#artist-dialog-note').textContent.includes('正在读取'));
    assert.equal(await page.locator('#artist-dialog-title').textContent(), topArtistName);
    assert.ok(await page.locator('#artist-dialog-tracks .analysis-track').count() > 0);
    await page.locator('#close-artist').click();
  }

  await page.locator('[data-analytics-tab="matrix"]').click();
  assert.equal(await page.locator('#view-matrix').isVisible(), true);
  assert.notEqual(await page.locator('#matrix-daily-avg').textContent(), '—');
  assert.notEqual(await page.locator('#matrix-diversity').textContent(), '—');

  // 验证开放式插槽扩展 API
  await page.evaluate(() => {
    window.CollectorMusicAnalytics.registerSlotComponent((slot, d) => {
      const p = document.createElement('p');
      p.id = 'custom-slot-probe';
      p.textContent = `Plugin loaded: ${d.records.length} records`;
      slot.append(p);
    });
  });
  assert.equal(await page.locator('#custom-slot-probe').count(), 1);

  await page.locator('[data-analytics-tab="trend"]').click();
  assert.equal(await page.locator('#view-trend').isVisible(), true);

  await page.locator('[data-collection="likes"]').click();
  assert.equal(await page.locator('.favorite-row').count(), Math.min(10, data.likes.length));
  if (data.likes.slice(0,10).some(like => like.track_duration_ms > 0)) {
    assert.match(await page.locator('#collection-list').textContent(), /曲长 \d+:\d{2}/);
  }
  if (data.likes.length > 10) {
    await page.locator('#collection-next').click();
    assert.match(await page.locator('#collection-page-info').textContent(), /^11/);
  }
  if (data.likes.length) {
    const lastLike = data.likes.at(-1);
    await page.locator('#collection-search').fill(lastLike.track_id);
    const likeLink = page.locator(`.favorite-row a[href="https://music.163.com/#/song?id=${lastLike.track_id}"]`);
    assert.equal(await likeLink.count(), 1);
    assert.equal(await likeLink.getAttribute('target'), '_blank');
    assert.match(await likeLink.getAttribute('rel'), /noopener/);
    await page.locator('#collection-search').fill('not-a-song-3891057');
    assert.equal(await page.locator('.favorite-row').count(), 0);
    await page.locator('#collection-search').fill('');
  }
  await page.locator('[data-collection="playlists"]').click();
  await page.locator('#collection-kind').selectOption('subscribed');
  assert.equal(await page.locator('.playlist-card').count(), Math.min(10,data.playlists.filter(p=>p.kind === 'subscribed').length));
  await page.locator('#collection-kind').selectOption('all');
  for (const link of await page.locator('.song-link').all()) {
    const href = await link.getAttribute('href');
    assert.ok(href.startsWith('https://music.163.com/#/'));
  }
  assert.equal(await page.locator('#latest-track a').count(), data.latest_device ? 1 : 0);
  await page.locator('#help').click();
  assert.equal(await page.locator('#help-dialog').isVisible(), true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#help-dialog').isVisible(), false);
  await page.locator('#refresh').click();
  await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
  await checkUsability(page, data);
  if (!desktopOnly) {
    await page.setViewportSize({width: 390, height: 844});
    await page.goto(base, {waitUntil: 'networkidle'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path: '.local/dashboard-qa/mobile.png', fullPage: true});
    await page.locator('[data-collection="likes"]').click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('.collection-panel').screenshot({path: '.local/dashboard-qa/likes-mobile.png'});
  }
  // Source text must remain inert even if an upstream title contains HTML.
  const injected = structuredClone(data);
  injected.records[0].track = '<img src=x onerror="window.injected=true">';
  injected.records[0].platform_track_id = 'javascript:alert(1)';
  await page.route('**/api/dashboard*', route => route.fulfill({json: injected}));
  await page.goto(base, {waitUntil: 'networkidle'});
  assert.equal(await page.locator('#records-body img').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  assert.ok((await page.locator('#records-body').textContent()).includes('<img src=x'));
  assert.ok((await page.locator('#records-body a').first().getAttribute('href')).startsWith('https://music.163.com/#/search/'));
  await page.unroute('**/api/dashboard*');
  // Source, time-zone and full-history aggregation are tested through the real analytics API.
  await page.route('**/api/dashboard*', route => route.fulfill({json: {status:'empty', message:'尚未发布（测试）'}}));
  await page.locator('#refresh').click();
  await page.waitForFunction(() => !document.querySelector('#refresh').disabled);
  assert.equal(await page.locator('#notice').textContent(), '尚未发布（测试）');
  assert.equal(await page.locator('#stat-pano').textContent(), '—');
  assert.equal(await page.locator('#latest-track').textContent(), '等待第一条设备记录');
  assert.equal(await page.locator('#chart rect').count(), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(outsideRequests, []);
  console.log(`Browser checks passed: ${desktopOnly ? 'desktop only' : 'desktop and mobile'}, real data, shortcuts, date/sort/size filters, keyboard selects, page jumps, resets, refresh retention, charts, collections, dialog, XSS, empty state, no external requests.`);
} finally {
  await browser.close();
}
