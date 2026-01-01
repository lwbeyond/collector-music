'use strict';

let analysisRequest = 0;
let analysisController;
let artistController;
const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const dayMillis = 86400000;
let catalogPage = 0;
const tagControls = {genre: 'filter-genre', language: 'filter-language', tag: 'filter-tag', q: 'filter-song-search'};
let tagSearchTimer;
const percent = (value, positiveCount = 0) => value == null ? '—'
  : value === 0 && positiveCount > 0 ? '<0.1%' : `${number.format(value)}%`;

function clearAnalytics(message = '') {
  analysisRequest++;
  analysisController?.abort();
  artistController?.abort();
  $('artist-dialog').close();
  state.analysis = null;
  $('analytics').removeAttribute('aria-busy');
  $('analysis-export').disabled = true;
  $('analysis-retry').hidden = true;
  $('chart-tooltip').hidden = true;
  document.querySelectorAll('.analytics-view').forEach(view => { view.style.minHeight = ''; });
  for (const id of ['chart', 'rhythm-bars', 'rhythm-heatmap', 'artists-chart', 'analysis-tracks', 'analysis-insights', 'artist-dialog-tracks']) $(id).replaceChildren();
  for (const element of document.querySelectorAll('.matrix-value')) element.textContent = '—';
  for (const id of ['chart-range', 'rhythm-highlight', 'rhythm-peak-time', 'artists-summary-label', 'matrix-peak-date', 'matrix-calendar-days', 'analysis-comparison']) setText(id, '');
  setText('chart-total', '—');
  setText('analysis-status', message);
  setText('rhythm-cell-detail', '');
  $('catalog-body').replaceChildren();
  setText('catalog-summary', '歌曲清单');
  setText('catalog-page-info', '');
  $('catalog-prev').disabled = $('catalog-next').disabled = true;
  $('catalog-empty').hidden = true;
}

function renderTagFilters(catalog) {
  const facets = catalog?.facets || {};
  for (const [field, id, title] of [['genre', 'filter-genre', '全部曲风'], ['language', 'filter-language', '全部语种'], ['recommended_tags', 'filter-tag', '全部标签']]) {
    const select = $(id), selected = select.value;
    const options = [new Option(title, ''), ...(facets[field] || []).map(item => new Option(`${item.value} · ${count(item.songs)}`, item.value))];
    if (selected && !options.some(option => option.value === selected)) options.push(new Option(`${selected} · 0`, selected));
    select.replaceChildren(...options);
    select.value = selected;
    select.dispatchEvent(new Event('selectsync'));
  }
  const statuses = catalog?.states || {};
  const notes = [];
  if (statuses.uncollected) notes.push(`待补采 ${count(statuses.uncollected)} 首`);
  if (statuses.unavailable) notes.push(`采集失败 ${count(statuses.unavailable)} 首`);
  if (statuses.empty) notes.push(`平台标签全空 ${count(statuses.empty)} 首`);
  if (statuses.stale) notes.push(`使用旧标签 ${count(statuses.stale)} 首`);
  setText('tag-coverage-note', notes.length ? '全库：' + notes.join(' · ') : '');
  $('tag-coverage-note').hidden = !notes.length;
}

function renderCatalog() {
  const data = state.analysis;
  if (!data) return;
  const songs = data.catalog || [], size = 8, pages = Math.max(1, Math.ceil(songs.length / size));
  catalogPage = Math.min(catalogPage, pages - 1);
  setText('catalog-summary', `歌曲清单 · ${count(songs.length)} 首 · 本期有记录 ${count(data.catalog_observed || 0)} 首`);
  const body = $('catalog-body');
  body.replaceChildren();
  for (const song of songs.slice(catalogPage * size, (catalogPage + 1) * size)) {
    const row = node('tr'), detail = node('td');
    const link = songLink({...song, track: song.track || `网易云歌曲 ${song.platform_track_id}`});
    link.title += ` · ID ${song.platform_track_id}`;
    detail.append(link, node('div', 'catalog-artist', [song.artist || '艺人未提供', song.album].filter(Boolean).join(' · ')));
    const warning = {uncollected: '待补采', unavailable: '采集失败', empty: '平台未提供标签', stale: '旧标签 · 最近采集失败'}[song.tag_status];
    if (warning) detail.append(node('span', 'catalog-tag-state', warning));
    row.append(detail);
    for (const field of ['genre', 'language', 'recommended_tags']) {
      const values = song.tags[field] || [];
      const cell = node('td', 'catalog-labels', values.length ? values.join(' / ') : '—');
      if (!values.length) cell.title = song.tag_data_status ? '平台未提供此字段' : '尚无可用的采集结果';
      row.append(cell);
    }
    row.append(node('td', 'catalog-records', song.records ? count(song.records) : '未记录'));
    body.append(row);
  }
  $('catalog-empty').hidden = songs.length > 0;
  setText('catalog-empty', Object.values(tagControls).some(id => $(id).value) ? '没有符合这些条件的歌曲，可减少条件或清空筛选。' : '当前发布尚无已知的网易云歌曲。');
  setText('catalog-page-info', songs.length ? `${catalogPage + 1} / ${pages} 页` : '');
  $('catalog-prev').disabled = catalogPage === 0;
  $('catalog-next').disabled = catalogPage >= pages - 1;
}

function analysisParams() {
  const period = $('period').value;
  const params = new URLSearchParams({generation: state.data.generation, source: state.chartSource, period});
  for (const [key, id] of Object.entries(tagControls)) if ($(id).value.trim()) params.set(key, $(id).value.trim());
  if (period === 'custom') {
    const start = $('analysis-start').value, end = $('analysis-end').value;
    if (!start || !end || start > end || end > state.data.today) {
      throw new Error('请选择有效的起止日期，开始不晚于结束，结束不晚于今天。');
    }
    params.set('start', start);
    params.set('end', end);
  }
  return params;
}

async function renderAnalytics() {
  clearTimeout(tagSearchTimer);
  $('tag-filter-reset').disabled = !Object.values(tagControls).some(id => $(id).value);
  const currentView = document.querySelector('.analytics-view.active');
  const previousHeight = currentView?.getBoundingClientRect().height || 0;
  clearAnalytics();
  $('analysis-dates').hidden = $('period').value !== 'custom';
  if (!state.data) {
    setText('analysis-status', '尚无已发布数据，请先导入并发布。');
    return;
  }
  for (const id of ['analysis-start', 'analysis-end']) $(id).max = state.data.today;
  if (!$('analysis-start').value) $('analysis-start').value = isoDay(Date.parse(state.data.today) - 29 * dayMillis);
  if (!$('analysis-end').value) $('analysis-end').value = state.data.today;
  let params;
  try { params = analysisParams(); }
  catch (error) { setText('analysis-status', error.message); return; }
  if (currentView) currentView.style.minHeight = previousHeight + 'px';
  const request = analysisRequest;
  analysisController = new AbortController();
  $('analytics').setAttribute('aria-busy', 'true');
  setText('analysis-status', '正在统计…');
  setText('analysis-scope-note', '各来源独立统计；未记录时段覆盖未知。');
  try {
    const response = await fetch('/api/analytics?' + params, {cache: 'no-store', signal: analysisController.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '分析读取失败，请重试。');
    if (request !== analysisRequest) return;
    if (data.status === 'empty') throw new Error('已发布数据已移除，请刷新展示。');
    if (data.generation !== state.data.generation) throw new Error('数据版本已变化，请刷新展示。');
    state.analysis = data;
    state.data.today = data.today;
    for (const id of ['analysis-start', 'analysis-end']) $(id).max = data.today;
    const s = data.summary;
    const exclusions = data.excluded.invalid_time + data.excluded.future;
    setText('analysis-scope-note', '各来源独立统计；歌曲按网易云 ID 关联，筛选条件取交集，多标签不重复计数。' +
      (data.tag_unlinked_records ? ` 本期 ${count(data.tag_unlinked_records)} 条观测没有可关联的网易云 ID，无法纳入歌曲标签筛选。` : ''));
    setText('analysis-status', `${data.start} — ${data.end} · ${data.timezone}` +
      (s.total ? (s.total < 20 ? ' · 样本较少' : '') : ' · 此范围没有可分析记录') +
      (exclusions ? ` · 未纳入 ${count(exclusions)} 条时间缺失、无效或晚于今天的观测` : ''));
    $('analysis-export').disabled = s.total === 0;
    const c = data.comparison;
    setText('analysis-comparison', c
      ? `前期 ${c.start} — ${c.end} · ${count(c.total)} 条；` +
        (c.total ? `本期 ${c.difference >= 0 ? '+' : '−'}${count(Math.abs(c.difference))} 条（${c.change_percent >= 0 ? '+' : ''}${percent(c.change_percent)}）。` : '无前期记录，变化率未知。') + '采集覆盖变化会影响对比。'
      : '');
    renderChart();
    renderRhythm();
    renderArtists();
    renderMatrix();
    renderCatalog();
  } catch (error) {
    if (request !== analysisRequest || error.name === 'AbortError') return;
    setText('analysis-status', error.message);
    $('analysis-retry').hidden = false;
  } finally {
    if (request === analysisRequest) {
      $('analytics').removeAttribute('aria-busy');
      if (currentView) currentView.style.minHeight = '';
    }
  }
}

function renderChart() {
  const data = state.analysis;
  if (!data) return;
  const svg = $('chart'), start = Date.parse(data.start), days = data.summary.calendar_days;
  const step = Math.max(1, Math.ceil(days / 58));
  const bins = Array.from({length: Math.ceil(days / step)}, (_, i) => ({
    from: isoDay(start + i * step * dayMillis),
    to: isoDay(start + (Math.min(days, (i + 1) * step) - 1) * dayMillis), value: 0
  }));
  for (const item of data.daily) bins[Math.floor((Date.parse(item.date) - start) / dayMillis / step)].value += item.records;
  setText('chart-total', count(data.summary.total));
  setText('chart-range', step === 1 ? '按日' : `每 ${step} 天汇总`);
  const ceiling = Math.ceil(Math.max(1, ...bins.map(b => b.value)) / 4) * 4;
  const width = Math.max(280, Math.min(1120, $('analytics').clientWidth));
  svg.setAttribute('viewBox', `0 0 ${width} 220`);
  const left = 48, right = width - 24, baseline = 187, height = 160, slot = (right - left) / bins.length;
  svg.replaceChildren(
    svgNode('title', {id: 'chart-title'}, `${sourceName(data.source)} · ${data.summary.total} 条观测`),
    svgNode('desc', {id: 'chart-desc'}, '按日期统计完整已发布观测。空心短线表示没有记录，覆盖未知，不能视为零次播放。')
  );
  for (let i = 0; i <= 4; i++) {
    const y = baseline - i * height / 4;
    svg.append(svgNode('line', {x1: left, x2: right, y1: y, y2: y, class: 'gridline'}),
      svgNode('text', {x: left - 10, y: y + 3, 'text-anchor': 'end'}, count(ceiling * i / 4)));
  }
  bins.forEach((bin, i) => {
    const h = bin.value ? Math.max(4, bin.value / ceiling * height) : 3;
    const title = `${bin.from}${bin.from !== bin.to ? ' 至 ' + bin.to : ''}\n${bin.value ? count(bin.value) + ' 条已记录观测' : '没有记录，覆盖未知'}`;
    const bar = svgNode('rect', {x: left + i * slot + 2, y: baseline - h, width: Math.max(2, slot - 4), height: h, rx: 2,
      class: bin.value ? 'bar' : 'missing', tabindex: 0, role: 'img', 'aria-label': title});
    bar.append(svgNode('title', {}, title));
    const show = () => {
      const bounds = bar.getBoundingClientRect(), parent = svg.parentElement.getBoundingClientRect();
      const tip = $('chart-tooltip');
      tip.textContent = title;
      tip.style.left = Math.max(0, Math.min(parent.width - 210, bounds.x - parent.x)) + 'px';
      tip.style.top = Math.max(0, bounds.y - parent.y - 59) + 'px';
      tip.hidden = false;
    };
    for (const event of ['mouseenter', 'focus', 'click']) bar.addEventListener(event, show);
    for (const event of ['mouseleave', 'blur']) bar.addEventListener(event, () => { $('chart-tooltip').hidden = true; });
    svg.append(bar);
  });
  for (const i of new Set([0, Math.floor((bins.length - 1) / 2), bins.length - 1])) {
    svg.append(svgNode('text', {x: left + (i + 0.5) * slot, y: 212, 'text-anchor': 'middle', class: 'axis'}, bins[i].from.slice(5).replace('-', '.')));
  }
}

function renderRhythm() {
  const data = state.analysis;
  if (!data) return;
  const container = $('rhythm-bars');
  container.replaceChildren();
  const max = Math.max(1, ...data.hours);
  const peaks = data.hours.flatMap((n, h) => n && n === max ? [String(h).padStart(2, '0') + ':00'] : []);
  setText('rhythm-highlight', peaks.length ? `最多 ${peaks.join('、')} · ${count(max)} 条 / 时段` : '此范围没有时段记录。');
  setText('rhythm-peak-time', `00:00–05:59 占 ${percent(data.summary.night_percent, data.summary.night_records)}`);
  data.hours.forEach((n, hour) => {
    const col = node('div', 'rhythm-col');
    const pillar = node('button', `rhythm-pillar ${!n ? 'missing' : n / max >= .65 ? 'high' : n / max >= .25 ? 'medium' : 'low'}`);
    pillar.type = 'button';
    pillar.style.height = `${Math.max(3, n / max * 135)}px`;
    pillar.title = `${String(hour).padStart(2, '0')}:00–${String(hour + 1).padStart(2, '0')}:00\n${n} 条记录${n ? '' : '（覆盖未知）'}`;
    pillar.setAttribute('aria-label', pillar.title);
    pillar.dataset.count = n;
    for (const event of ['click', 'focus']) pillar.addEventListener(event, () => setText('rhythm-cell-detail', pillar.title));
    col.append(pillar, node('span', 'rhythm-hour-label', hour));
    container.append(col);
  });
  const grid = $('rhythm-heatmap');
  grid.replaceChildren(node('span'));
  for (let h = 0; h < 24; h++) grid.append(node('span', 'heatmap-hour', h % 3 === 0 ? String(h).padStart(2, '0') : ''));
  const peak = Math.max(1, ...data.heatmap.flat());
  data.heatmap.forEach((hours, day) => {
    grid.append(node('span', 'heatmap-day', weekdays[day]));
    hours.forEach((n, h) => {
      const cell = node('button', `heatmap-cell level-${n ? Math.max(1, Math.ceil(n / peak * 4)) : 0}`);
      cell.type = 'button';
      cell.dataset.count = n;
      cell.title = `${weekdays[day]} ${String(h).padStart(2, '0')}:00–${String(h + 1).padStart(2, '0')}:00 · ${count(n)} 条观测${n ? '' : '，覆盖未知'}`;
      cell.setAttribute('aria-label', cell.title);
      cell.tabIndex = day === 0 && h === 0 ? 0 : -1;
      for (const event of ['click', 'focus']) cell.addEventListener(event, () => {
        grid.querySelector('[tabindex="0"]')?.setAttribute('tabindex', '-1');
        cell.tabIndex = 0;
        setText('rhythm-cell-detail', cell.title);
      });
      cell.addEventListener('keydown', event => {
        const delta = {ArrowRight: 1, ArrowLeft: -1, ArrowDown: 24, ArrowUp: -24}[event.key];
        if (delta == null) return;
        event.preventDefault();
        grid.querySelectorAll('button')[(day * 24 + h + delta + 168) % 168].focus();
      });
      grid.append(cell);
    });
  });
}

function renderTrackList(container, tracks) {
  container.replaceChildren();
  for (const [i, item] of tracks.entries()) {
    const row = node('div', 'analysis-track');
    const detail = node('div', 'analysis-track-detail');
    detail.append(songLink(item), node('p', 'track-artist', [item.artist, item.album].filter(Boolean).join(' · ')));
    row.append(node('span', 'artist-rank', String(i + 1).padStart(2, '0')), detail,
      node('span', 'artist-count', `${count(item.records)} 条 · ${percent(item.share, item.records)}`));
    container.append(row);
  }
  if (!tracks.length) container.append(node('p', 'panel-note', '此范围暂无曲目记录。'));
}

function renderArtists() {
  const data = state.analysis;
  if (!data) return;
  const container = $('artists-chart');
  container.replaceChildren();
  for (const [index, item] of data.artists.entries()) {
    const row = node('button', 'artist-bar-item');
    row.type = 'button';
    const name = node('span', 'artist-name', item.artist);
    name.title = item.artist;
    const wrap = node('span', 'artist-track-fill-wrap'), fill = node('span', 'artist-track-fill');
    fill.style.width = `${item.records / data.artists[0].records * 100}%`;
    wrap.append(fill);
    row.append(node('span', `artist-rank top${index + 1}`, `#${index + 1}`), name, wrap,
      node('span', 'artist-count', `${count(item.records)} 条 · ${percent(item.share, item.records)}`));
    row.addEventListener('click', () => showArtist(item.artist));
    container.append(row);
  }
  if (!data.artists.length) container.append(node('p', 'panel-note', '此范围暂无已知艺人信息。'));
  setText('artists-summary-label', `${count(data.summary.unique_artists)} 个艺人/组合标签` +
    (data.summary.unknown_artist_records ? ` · ${count(data.summary.unknown_artist_records)} 条观测未提供艺人` : ''));
  renderTrackList($('analysis-tracks'), data.tracks);
}

async function showArtist(artist) {
  const data = state.analysis;
  if (!data) return;
  artistController?.abort();
  const controller = artistController = new AbortController();
  const params = new URLSearchParams({generation: data.generation, source: data.source, period: 'custom', start: data.start, end: data.end, artist});
  for (const [key, value] of Object.entries(data.filters || {})) if (value) params.set(key, value);
  setText('artist-dialog-title', artist);
  setText('artist-dialog-note', '正在读取曲目…');
  $('artist-dialog-tracks').replaceChildren();
  $('artist-dialog').showModal();
  try {
    const response = await fetch('/api/analytics?' + params, {cache: 'no-store', signal: controller.signal});
    const detail = await response.json();
    if (!response.ok) throw new Error(detail.error || '明细读取失败，请关闭后重试。');
    if (controller.signal.aborted || state.analysis !== data) return;
    if (detail.status !== 'ready' || detail.generation !== data.generation) throw new Error('数据版本已变化，请刷新展示。');
    setText('artist-dialog-note', `${sourceName(data.source)} · ${data.start} — ${data.end} · ${count(detail.summary.total)} 条 / ${count(detail.summary.unique_tracks)} 首 · 前 10 首`);
    renderTrackList($('artist-dialog-tracks'), detail.tracks);
  } catch (error) {
    if (!controller.signal.aborted) setText('artist-dialog-note', error.message);
  }
}

function renderMatrix() {
  const data = state.analysis;
  if (!data) return;
  const s = data.summary;
  setText('matrix-daily-avg', s.daily_average == null ? '—' : s.daily_average.toFixed(1));
  setText('matrix-diversity', percent(s.diversity_percent, s.unique_tracks));
  setText('matrix-artists-count', count(s.unique_artists));
  setText('matrix-peak-day', s.total ? count(s.peak_count) : '—');
  setText('matrix-peak-date', s.peak_date || '暂无记录');
  setText('matrix-active-days', count(s.active_days));
  setText('matrix-calendar-days', `所选 ${count(s.calendar_days)} 天 · 其余日期覆盖未知`);
  setText('matrix-tracks', count(s.unique_tracks));
  setText('matrix-streak', `${count(s.longest_streak)} 天`);
  setText('matrix-night', percent(s.night_percent, s.night_records));
  const insights = $('analysis-insights');
  insights.replaceChildren();
  const lines = s.total ? [
    `共记录 ${count(s.total)} 条观测，涉及 ${count(s.unique_tracks)} 首曲目；同一曲目标识在首次之后又出现 ${count(s.repeat_records)} 条观测。`,
    `最密集的记录日在 ${s.peak_date}，共 ${count(s.peak_count)} 条；最长连续 ${count(s.longest_streak)} 天有记录。`,
    data.artists.length ? `出现最多的艺人标签是「${data.artists[0].artist}」，占当前范围观测的 ${percent(data.artists[0].share)}。` : '艺人信息不足，暂不归纳艺人偏好。'
  ] : ['所选来源在此范围没有可分析的观测。可以扩大日期范围，或在接收、采集并发布后刷新展示。'];
  for (const line of lines) insights.append(node('li', '', line));
}

function exportAnalysis() {
  const data = state.analysis;
  if (!data?.summary.total) return;
  const filters = data.filters || {};
  const rows = [['日期', '来源', '已记录观测条数', '时区', '覆盖状态', '分析开始日期', '分析结束日期', '发布版本', '曲风筛选', '语种筛选', '推荐标签筛选', '歌曲搜索'],
    ...data.daily.map(d => [d.date, data.source, d.records, data.timezone, 'observed_only', data.start, data.end, data.generation, filters.genre || '', filters.language || '', filters.tag || '', filters.q || ''])];
  const csv = '\uFEFF' + rows.map(row => row.map(value => `"${String(value).replaceAll('"', '""')}"`).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
  const link = node('a');
  link.href = url;
  link.download = `music-daily-${data.source}-${data.start}-${data.end}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function setupAnalyticsTabs() {
  for (const id of ['filter-genre', 'filter-language', 'filter-tag']) $(id).addEventListener('change', () => { catalogPage = 0; renderAnalytics(); });
  $('filter-song-search').addEventListener('input', () => {
    clearTimeout(tagSearchTimer);
    catalogPage = 0;
    // Clear old graphs immediately; an in-flight response must not restore them.
    clearAnalytics('正在筛选…');
    $('tag-filter-reset').disabled = false;
    tagSearchTimer = setTimeout(renderAnalytics, 180);
  });
  $('tag-filter-reset').addEventListener('click', () => {
    for (const id of Object.values(tagControls)) { $(id).value = ''; $(id).dispatchEvent(new Event('selectsync')); }
    catalogPage = 0;
    renderAnalytics();
  });
  $('catalog-prev').addEventListener('click', () => { catalogPage--; renderCatalog(); });
  $('catalog-next').addEventListener('click', () => { catalogPage++; renderCatalog(); });
  const tabs = [...document.querySelectorAll('[data-analytics-tab]')];
  function activate(tab) {
    state.analyticsTab = tab.dataset.analyticsTab;
    for (const t of tabs) {
      const active = t === tab;
      t.classList.toggle('active', active);
      t.setAttribute('aria-selected', String(active));
      t.tabIndex = active ? 0 : -1;
      const view = $('view-' + t.dataset.analyticsTab);
      view.hidden = !active;
      view.classList.toggle('active', active);
    }
  }
  for (const [i, tab] of tabs.entries()) {
    tab.id = 'tab-' + tab.dataset.analyticsTab;
    $('view-' + tab.dataset.analyticsTab).setAttribute('aria-labelledby', tab.id);
    tab.addEventListener('click', () => activate(tab));
    tab.addEventListener('keydown', event => {
      const next = event.key === 'ArrowRight' ? (i + 1) % tabs.length : event.key === 'ArrowLeft' ? (i + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
      if (next == null) return;
      event.preventDefault();
      activate(tabs[next]);
      tabs[next].focus();
    });
  }
  activate(tabs[0]);
  $('analysis-export').addEventListener('click', exportAnalysis);
  $('analysis-retry').addEventListener('click', renderAnalytics);
  for (const id of ['analysis-start', 'analysis-end']) $(id).addEventListener('change', renderAnalytics);
  $('close-artist').addEventListener('click', () => $('artist-dialog').close());
  $('artist-dialog').addEventListener('close', () => artistController?.abort());
  let chartWidth = 0;
  new ResizeObserver(([entry]) => {
    const width = Math.round(entry.contentRect.width);
    if (width !== chartWidth) {
      chartWidth = width;
      renderChart();
    }
  }).observe($('analytics'));
}
