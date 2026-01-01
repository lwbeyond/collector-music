'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  data: null,
  page: 0,
  chartSource: 'netease_recent',
  collection: 'playlists',
  rankingLimit: 8,
  collectionPage: 0,
  analyticsTab: 'trend'
};

const number = new Intl.NumberFormat('zh-CN');
const count = value => value == null ? '—' : number.format(value);
const node = (tag, cls, text) => {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (text != null) element.textContent = text;
  return element;
};
const setText = (id, text) => {
  const el = $(id);
  if (el) el.textContent = text;
};

const sourceName = source => source === 'pano' ? '设备记录' : '网易云近期';
const deviceName = key => ({
  phone: '手机',
  tablet: '平板'
}[key] || key || '未知设备');

const trackLength = value => Number.isFinite(value) && value >= 0
  ? `${Math.floor(value / 60000)}:${String(Math.floor(value / 1000) % 60).padStart(2, '0')}`
  : null;

const platformId = value => /^[1-9]\d*$/.test(String(value || ''));

function externalLink(text, href, cls, title) {
  const link = node('a', cls, text);
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.referrerPolicy = 'no-referrer';
  link.title = title;
  return link;
}

function songLink(item, cls = 'track-name song-link') {
  const id = item.track_id || item.platform_track_id;
  const title = item.track || '曲目信息暂未取得';
  const exact = platformId(id);
  const href = exact
    ? `https://music.163.com/#/song?id=${id}`
    : `https://music.163.com/#/search/m/?s=${encodeURIComponent([item.track, item.artist].filter(Boolean).join(' '))}&type=1`;
  return externalLink(
    title,
    href,
    cls,
    exact ? '在网易云打开这首歌 ↗' : '在网易云搜索歌名与艺人（设备记录没有歌曲 ID）↗'
  );
}

function dateParts(value) {
  if (!value) return { date: '时间未提供', time: '', hour: 0 };
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return { date: '时间未提供', time: '', hour: 0 };
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: state.data?.timezone || 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23'
  }).formatToParts(date);
  const p = Object.fromEntries(parts.map(x => [x.type, x.value]));
  return {
    date: `${p.year}.${p.month}.${p.day}`,
    time: `${p.hour}:${p.minute}`,
    hour: Number(p.hour || 0)
  };
}

function dateTime(value) {
  const d = dateParts(value);
  return d.date + (d.time ? ' ' + d.time : '');
}

function note(message, error = false) {
  const n = $('notice');
  n.hidden = !message;
  n.textContent = message || '';
  n.setAttribute('role', error ? 'alert' : 'status');
}

async function load(force = false) {
  const refreshBtn = $('refresh');
  refreshBtn.disabled = true;
  refreshBtn.setAttribute('aria-busy', 'true');
  setText('refresh-label', '读取中…');
  const previousGeneration = state.data?.generation;
  try {
    const response = await fetch('/api/dashboard' + (force ? '?refresh=1' : ''), { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '读取失败，请稍后重试。');

    if (data.status === 'empty') {
      state.data = null;
      window.CollectorMusicReports.render(null);
      clearAnalytics('尚无已发布数据，请先导入并发布。');
      for (const id of ['pano', 'recent', 'likes', 'playlists']) setText('stat-' + id, '—');
      for (const id of ['records-body', 'ranking-list', 'collection-list', 'source-grid', 'rhythm-bars', 'artists-chart']) {
        const el = $(id);
        if (el) el.replaceChildren();
      }
      $('records-empty').hidden = false;
      $('prev').disabled = $('next').disabled = true;
      setText('published-label', '等待首次归档');
      setText('record-count', '还没有已发布记录');
      setText('page-info', '0 条记录');
      setText('chart-total', '—');
      setText('latest-track', '等待第一条设备记录');
      setText('latest-artist', '等待同步设备播放记录');
      setText('latest-time', '设备听歌记录');
      setText('chart-range', '尚无已发布数据');
      setText('version-info', '尚无已发布数据');
      for (const id of ['matrix-daily-avg','matrix-diversity','matrix-artists-count','matrix-peak-day']) setText(id, '—');
      setText('analysis-scope-note', '尚无已发布数据');
      setText('record-scope-note', '尚无已发布数据');
      $('chart').replaceChildren(
        svgNode('title', { id: 'chart-title' }, '尚无已发布数据'),
        svgNode('desc', { id: 'chart-desc' }, '请先完成导入与发布。')
      );
      $('chart-tooltip').hidden = $('ranking-more').hidden = $('collection-pagination').hidden = true;
      setText('collection-page-info', '尚无已发布数据');
      setText('collection-note', '尚无已发布收藏。');
      setText('records-empty-title', '还没有已发布记录');
      setText('records-empty-hint', '点击顶部“如何更新”，完成接收或采集后再刷新展示。');
      setText('record-filter-label', '等待首次发布');
      $('record-page-form').hidden = true;
      $('record-reset').hidden = $('collection-reset').hidden = true;
      setText('ranking-note', '尚无已发布榜单。');
      setText('likes-note', '尚无已发布收藏');
      setText('playlists-note', '尚无已发布歌单');
      setText('matrix-daily-avg', '—');
      setText('matrix-diversity', '—');
      setText('matrix-artists-count', '—');
      setText('matrix-peak-day', '—');
      note(data.message);
      setText('refresh-label', '暂无发布');
      return;
    }

    state.data = data;
    note('');
    await render();
    setText('refresh-label', force ? previousGeneration === data.generation ? '已是最新展示' : '已更新展示' : '刷新展示');
  } catch (error) {
    note((state.data ? '暂时保留上次展示。' : '') + error.message, true);
    setText('refresh-label', '刷新失败 · 重试');
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.removeAttribute('aria-busy');
  }
}

async function render() {
  const data = state.data;
  if (!data) return;

  for (const key of ['pano', 'recent', 'likes', 'playlists']) setText('stat-' + key, count(data.stats[key]));
  setText('published-label', '最近发布 ' + dateTime(data.published_at));
  setText(
    'likes-note',
    data.scopes.likes.status === 'stale'
      ? '采集未完成 · 保留上次成功快照'
      : data.stats.likes == null
        ? '尚未采集，数量未知'
        : ''
  );
  setText(
    'playlists-note',
    data.scopes.playlists.status === 'stale'
      ? '采集未完成 · 保留上次成功快照'
      : data.stats.playlists == null
        ? '尚未采集，数量未知'
        : `创建 ${count(data.playlists.filter(p => p.kind === 'created').length)} · 收藏 ${count(data.playlists.filter(p => p.kind === 'subscribed').length)}`
  );

  const last = data.latest_device;
  $('latest-track').replaceChildren(last ? songLink(last, 'song-link') : document.createTextNode('等待第一条设备记录'));
  setText('latest-artist', last?.artist || '暂无设备播放记录');
  setText('latest-time', last ? `${deviceName(last.device_key)} · ${dateTime(last.occurred_at_utc)}` : '等待接收设备记录');
  setText('version-info', `时区 ${data.timezone} · 数据修订 ${data.data_revision} · 发布版本 ${data.generation}`);

  renderRecords();
  renderRankings();
  renderCollection();
  renderSources();
  window.CollectorMusicReports.render(data.platform_reports, data.timezone);
  renderTagFilters(data.song_catalog);
  await renderAnalytics();
}

function svgNode(tag, attributes, text) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) n.setAttribute(key, String(value));
  if (text != null) n.textContent = text;
  return n;
}

const isoDay = timestamp => new Date(timestamp).toISOString().slice(0, 10);

// Compatibility helper for extensions: this is only the loaded history slice.
// Full-publication metrics are available through getAnalysisData().
function analysisRecords() {
  if (!state.data || !state.analysis) return [];
  const end = Date.parse(state.analysis.end + 'T00:00:00Z');
  const start = Date.parse(state.analysis.start + 'T00:00:00Z');
  return state.data.records.filter(r => {
    if (r.source !== state.analysis.source || !['scrobble', 'recent_observation'].includes(r.event_kind) ||
        !r.occurred_at_utc || !Number.isFinite(Date.parse(r.occurred_at_utc))) return false;
    const day = Date.parse(dateParts(r.occurred_at_utc).date.replaceAll('.', '-') + 'T00:00:00Z');
    return day >= start && day <= end;
  });
}

/* 听歌记录列表 */
function renderRecords() {
  if (!state.data) return;
  const q = $('search').value.trim().toLocaleLowerCase();
  const source = $('record-source').value;
  const period = $('record-period').value;
  const end = state.data.today;
  const start = period === 'all' ? null : isoDay(Date.parse(end) - (Number(period) - 1) * 86400000);
  const filtered = state.data.records.filter(r =>
    (source === 'all' || r.source === source) &&
    (!q || [r.track, r.artist, r.album].filter(Boolean).join(' ').toLocaleLowerCase().includes(q)) &&
    (!start || (() => {
      const day = dateParts(r.occurred_at_utc).date.replaceAll('.', '-');
      return day >= start && day <= end;
    })())
  );
  const oldest = $('record-sort').value === 'oldest';
  filtered.sort((a, b) => {
    const left = Date.parse(a.occurred_at_utc), right = Date.parse(b.occurred_at_utc);
    if (!Number.isFinite(left)) return Number.isFinite(right) ? 1 : 0;
    if (!Number.isFinite(right)) return -1;
    return oldest ? left - right : right - left;
  });

  const pageSize = Number($('record-size').value);
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  state.page = Math.max(0, Math.min(state.page, pages - 1));
  const offset = state.page * pageSize;
  const body = $('records-body');
  body.replaceChildren();

  for (const [index, item] of filtered.slice(offset, offset + pageSize).entries()) {
    const tr = node('tr');
    const track = node('td', 'record-track'), album = node('td', 'record-album'), when = node('td', 'record-time'), sourceCell = node('td', 'record-source');
    track.append(songLink(item), node('div', 'track-artist', item.artist));
    track.title = `${item.track} — ${item.artist}`;
    album.append(node('div', 'album-name', item.album || '未提供专辑'));
    album.title = item.album || '';
    const date = dateParts(item.occurred_at_utc);
    when.append(node('div', 'time-date', date.date), node('div', 'time-clock', date.time));
    const badge = node('span', 'badge' + (item.source === 'pano' ? ' device' : ''), sourceName(item.source));
    sourceCell.append(badge);
    const related = (state.data.overlap_candidates || []).filter(link => link.left_id === item.id || link.right_id === item.id);
    if (related.length) {
      const ambiguous = related.some(link => link.status === 'ambiguous');
      const hint = node('div', 'track-artist', ambiguous ? '多条重合候选 · 未合并' : `疑似双源重合 · 相差 ${(related[0].time_delta_ms / 1000).toFixed(3)} 秒`);
      hint.dataset.overlap = ambiguous ? 'ambiguous' : 'candidate';
      hint.title = '仅标注候选：同人、网易云播放器、曲名/艺人/专辑一致，曲长差不超过 2 秒，时间差不超过 10 秒。平台时间语义未确认，原记录未删除或合并。';
      sourceCell.append(hint);
    }
    if (!['scrobble', 'recent_observation'].includes(item.event_kind)) {
      sourceCell.append(node('div', 'track-artist', item.event_kind === 'love' ? '喜欢操作' : '取消喜欢'));
    }
    tr.append(node('td', 'index-col', String(offset + index + 1).padStart(2, '0')), track, album, when, sourceCell);
    body.append(tr);
  }

  $('records-empty').hidden = filtered.length !== 0;
  setText('records-empty-title', '未找到匹配的记录');
  const filterActive = !!q || source !== 'netease_recent' || period !== 'all' || oldest;
  setText('records-empty-hint', state.data.records_truncated
    ? `仅搜索最近载入的 ${count(state.data.records.length)} 条记录。可重置筛选；更早记录的统计请查看数据分析。`
    : filterActive ? '试试更短的关键词，或点击“重置筛选”查看默认来源的记录。'
      : '当前来源暂无记录。可切换记录来源，或点击顶部“如何更新”查看接收与采集方法。');
  $('record-reset').hidden = !filterActive;
  setText('record-filter-label', [start ? `${start} — ${end}` : '',
    state.data.records_truncated ? `仅搜索最近 ${count(state.data.records.length)} 条；完整统计见数据分析` : ''].filter(Boolean).join(' · '));
  setText('record-count', `${count(filtered.length)} 条${state.data.records_truncated ? ' · 已载入范围' : ''}`);
  setText('record-scope-note', source === 'all'
    ? '同一次播放可能有两条来源记录，未作去重。'
    : source === 'pano' ? '达到记录阈值的设备播放，采集可能缺失。'
      : '平台近期观测，不代表完整播放流水。');
  setText('page-info', filtered.length ? `${offset + 1}–${Math.min(offset + pageSize, filtered.length)} / ${count(filtered.length)} 条` : '0 条记录');
  $('prev').disabled = state.page === 0;
  $('next').disabled = state.page >= pages - 1;
  updatePageJump('record', state.page, pages, filtered.length);
}

function updatePageJump(prefix, page, pages, total) {
  $(prefix + '-page-form').hidden = total === 0;
  $(prefix + '-page').value = page + 1;
  $(prefix + '-page').max = pages;
  setText(prefix + '-pages', `/ ${pages} 页`);
}

function setSelect(id, value) {
  $(id).value = value;
  $(id).dispatchEvent(new Event('selectsync'));
}

function resetRecords(source = 'netease_recent') {
  $('search').value = '';
  setSelect('record-source', source);
  setSelect('record-period', 'all');
  setSelect('record-sort', 'newest');
  state.page = 0;
  renderRecords();
}

/* 榜单列表 */
function renderRankings() {
  if (!state.data) return;
  const scope = $('ranking-scope').value;
  const entries = state.data.rankings[scope];
  const summary = state.data.scopes[scope];
  const list = $('ranking-list');
  list.replaceChildren();

  setText(
    'ranking-note',
    `${summary.status === 'stale' ? '采集未完成 · 保留上次快照 · ' : ''}平台报告次数${summary.observed_at ? ' · ' + dateParts(summary.observed_at).date : ''}`
  );

  for (const item of entries.slice(0, state.rankingLimit)) {
    const li = node('li');
    const detail = node('div', 'rank-detail');
    const playCount = node('div', 'rank-count', count(item.reported_play_count));
    detail.append(songLink(item), node('div', 'track-artist', item.artist));
    detail.title = item.track + ' — ' + item.artist;
    li.append(node('span', 'rank-number', String(item.position).padStart(2, '0')), detail, playCount);
    list.append(li);
  }

  if (!entries.length) {
    list.append(node('li', 'panel-note', summary.status === 'unavailable' ? '尚无成功采集的榜单。' : '这次榜单没有可见条目。'));
  }

  $('ranking-more').hidden = entries.length <= state.rankingLimit;
  $('ranking-more').textContent = `再看 ${Math.min(10, entries.length - state.rankingLimit)} 首 ↓`;
}

/* 收藏面板 */
function renderCollection() {
  if (!state.data) return;
  const list = $('collection-list');
  list.replaceChildren();

  const playlists = state.collection === 'playlists';
  const entries = playlists ? state.data.playlists : state.data.likes;
  const summary = state.data.scopes[state.collection];
  const q = $('collection-search').value.trim().toLocaleLowerCase();
  const kind = $('collection-kind').value;

  $('collection-kind').hidden = !playlists;
  $('collection-reset').hidden = !q && (!playlists || kind === 'all');
  $('collection-search').placeholder = playlists ? '搜索歌单名称' : '搜索红心歌名、艺人、专辑或 ID';

  const filtered = entries.filter(item =>
    (!playlists || kind === 'all' || item.kind === kind) &&
    (!q || [item.label, item.track, item.artist, item.album, item.track_id, item.entity_id].filter(Boolean).join(' ').toLocaleLowerCase().includes(q))
  );

  const size = 10;
  const pages = Math.max(1, Math.ceil(filtered.length / size));
  state.collectionPage = Math.max(0, Math.min(state.collectionPage, pages - 1));
  const offset = state.collectionPage * size;
  const resolved = entries.filter(item => item.track).length;

  setText(
    'collection-note',
    (summary.status === 'stale' ? '当前显示上次成功快照。' : '') +
    (playlists
      ? `${count(entries.length)} 个歌单 · 仅清单，未备份成员`
      : `${count(entries.length)} 首红心${resolved < entries.length ? ` · ${count(entries.length - resolved)} 首仅有 ID，详情未取得` : ''}`) +
    (summary.observed_at ? ' · ' + dateParts(summary.observed_at).date : '')
  );

  for (const [index, item] of filtered.slice(offset, offset + size).entries()) {
    if (playlists) {
      const card = node('article', 'playlist-card');
      const detail = node('div', 'playlist-detail');
      const heading = node('h3');
      if (platformId(item.entity_id)) {
        heading.append(externalLink(item.label || '未命名歌单', `https://music.163.com/#/playlist?id=${item.entity_id}`, 'song-link', '在网易云打开歌单 ↗'));
      } else {
        heading.textContent = item.label || '未命名歌单';
      }
      const kindLabel = { created: '我创建的', subscribed: '我收藏的', unknown: '歌单' }[item.kind] || '歌单';
      detail.append(heading, node('p', '', `${kindLabel}${item.track_count != null ? ' · ' + count(item.track_count) + ' 首' : ''}`));
      card.append(node('div', 'playlist-art', String(offset + index + 1).padStart(2, '0')), detail);
      list.append(card);
    } else {
      const row = node('div', 'favorite-row');
      const detail = node('div', '');
      detail.append(songLink(item), node('div', 'track-artist', item.artist || '已保留红心 ID · ' + item.track_id));
      const duration = trackLength(item.track_duration_ms);
      if (item.album || duration) {
        detail.append(node('div', 'track-artist', [item.album, duration ? '曲长 ' + duration : null].filter(Boolean).join(' · ')));
      }
      row.append(node('span', 'favorite-heart', '♡'), detail);
      list.append(row);
    }
  }

  if (!filtered.length) {
    list.append(node('p', 'panel-note', summary.status === 'unavailable' ? '这个范围尚未完成采集。' : entries.length ? '没有匹配的收藏，试试其他关键词。' : '这次快照中没有可见条目。'));
  }

  $('collection-pagination').hidden = false;
  setText('collection-page-info', filtered.length ? `${offset + 1}–${Math.min(offset + size, filtered.length)} / ${count(filtered.length)} 项` : '0 项');
  $('collection-prev').disabled = state.collectionPage === 0;
  $('collection-next').disabled = state.collectionPage >= pages - 1;
  updatePageJump('collection', state.collectionPage, pages, filtered.length);
}

/* 采集状态 */
function renderSources() {
  const grid = $('source-grid');
  grid.replaceChildren();
  const data = state.data;
  const devices = new Map(data.devices.map(d => [d.device_key, d]));

  function card(name, stateLabel, description, detail, time, pending = false) {
    const el = node('article', 'source-card' + (pending ? ' pending' : ''));
    const top = node('div', 'source-card-top');
    top.append(node('span', '', name), node('span', 'source-state', stateLabel));
    el.append(top, node('p', '', description));
    if (detail) el.append(node('p', '', detail));
    if (time) el.append(node('small', '', time));
    grid.append(el);
  }

  for (const key of [...new Set(['phone', ...devices.keys(), 'tablet'])]) {
    const device = devices.get(key);
    card(
      deviceName(key),
      device ? (device.records ? '已收到记录' : '等待播放记录') : '待接入',
      device ? `${count(device.records)} 条达到阈值的播放` : '尚未收到这台设备的日志',
      '',
      device ? '最近接收 ' + dateTime(device.last_received) : '',
      !device
    );
  }

  const scopes = Object.values(data.scopes);
  const successful = scopes.filter(s => s.status === 'ok').length;
  const last = scopes.map(s => s.latest_attempt_at).filter(Boolean).sort().at(-1);
  card(
    '网易云账号',
    successful === 5 ? '快照已归档' : last ? '部分范围待更新' : '待采集',
    `${successful} / 5 个范围最近采集成功`,
    '近期 · 周榜 · 总榜 · 红心 · 歌单',
    last ? '最近采集 ' + dateTime(last) : '先通过本机二维码登录',
    !last
  );
}

/* ==========================================================================
   分析维度切换器及前台开放扩展框架 API
   ========================================================================== */
function enhanceSelects() {
  const selects = document.querySelectorAll('select');
  selects.forEach(select => {
    if (select.dataset.enhanced) return;
    select.dataset.enhanced = 'true';

    const wrap = node('div', 'custom-select-wrap');
    if (select.id) wrap.id = 'wrap-' + select.id;
    wrap.hidden = select.hidden;

    const trigger = node('button', 'custom-select-trigger');
    trigger.type = 'button';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    const fieldLabels = [...select.labels];
    const fieldLabel = select.getAttribute('aria-label') || fieldLabels.map(label => label.textContent.trim()).join(' ') || '选择选项';
    trigger.id = 'select-' + select.id;
    fieldLabels.forEach(label => { label.htmlFor = trigger.id; });

    const labelSpan = node('span', 'custom-select-label');
    const arrowSpan = node('span', 'custom-select-arrow');
    arrowSpan.setAttribute('aria-hidden', 'true');
    arrowSpan.innerHTML = '<svg viewBox="0 0 12 12"><path d="M2.5 4.5L6 8L9.5 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    trigger.append(labelSpan, arrowSpan);

    const dropdown = node('div', 'custom-select-dropdown');
    dropdown.setAttribute('role', 'listbox');
    dropdown.id = 'options-' + select.id;
    dropdown.setAttribute('aria-label', fieldLabel);
    trigger.setAttribute('aria-controls', dropdown.id);
    dropdown.hidden = true;

    function buildOptions() {
      dropdown.replaceChildren();
      const options = Array.from(select.options);
      const selectedIndex = Math.max(0, select.selectedIndex);
      const currentOpt = options[selectedIndex] || options[0];
      labelSpan.textContent = currentOpt ? currentOpt.textContent : '';
      trigger.setAttribute('aria-label', `${fieldLabel}：${labelSpan.textContent}`);

      options.forEach(opt => {
        const item = node('div', 'custom-select-option' + (opt.value === select.value ? ' selected' : ''));
        item.setAttribute('role', 'option');
        item.dataset.value = opt.value;
        item.setAttribute('aria-selected', String(opt.value === select.value));
        item.tabIndex = opt.value === select.value ? 0 : -1;

        const text = node('span', 'option-text', opt.textContent);
        const check = node('span', 'option-check', '✓');
        check.setAttribute('aria-hidden', 'true');
        item.append(text, check);

        item.addEventListener('click', (e) => {
          e.stopPropagation();
          if (select.value !== opt.value) {
            select.value = opt.value;
            select.dispatchEvent(new Event('change', { bubbles: true }));
          }
          closeDropdown();
          trigger.focus();
        });

        dropdown.append(item);
      });
    }

    function openDropdown() {
      document.querySelectorAll('.custom-select-dropdown:not([hidden])').forEach(d => {
        d.hidden = true;
        const t = d.previousElementSibling;
        if (t) t.setAttribute('aria-expanded', 'false');
        const w = d.closest('.custom-select-wrap');
        if (w) w.classList.remove('open');
      });
      dropdown.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      wrap.classList.add('open');
      dropdown.querySelector('[aria-selected="true"]')?.focus();
    }

    function closeDropdown() {
      dropdown.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
      wrap.classList.remove('open');
    }

    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      if (dropdown.hidden) openDropdown();
      else closeDropdown();
    });

    trigger.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openDropdown();
      } else if (e.key === 'Escape') {
        closeDropdown();
      }
    });

    dropdown.addEventListener('keydown', (e) => {
      const items = [...dropdown.querySelectorAll('[role="option"]')];
      const index = items.indexOf(document.activeElement);
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
        e.preventDefault();
        const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : (index + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        items[index]?.click();
      }
      if (e.key === 'Escape') {
        closeDropdown();
        trigger.focus();
      }
    });

    select.addEventListener('change', () => {
      buildOptions();
    });
    select.addEventListener('selectsync', buildOptions);
    wrap.addEventListener('focusout', event => {
      if (!wrap.contains(event.relatedTarget)) closeDropdown();
    });

    const observer = new MutationObserver(() => {
      wrap.hidden = select.hidden;
      trigger.disabled = select.disabled;
      buildOptions();
    });
    observer.observe(select, { attributes: true, attributeFilter: ['hidden', 'disabled'] });

    buildOptions();

    select.classList.add('custom-select-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    select.parentNode.insertBefore(wrap, select);
    wrap.append(trigger, dropdown, select);
  });

  if (!window._customSelectGlobalClickBound) {
    window._customSelectGlobalClickBound = true;
    document.addEventListener('click', () => {
      document.querySelectorAll('.custom-select-dropdown:not([hidden])').forEach(d => {
        d.hidden = true;
        const t = d.previousElementSibling;
        if (t) t.setAttribute('aria-expanded', 'false');
        const w = d.closest('.custom-select-wrap');
        if (w) w.classList.remove('open');
      });
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        document.querySelectorAll('.custom-select-dropdown:not([hidden])').forEach(d => {
          d.hidden = true;
          const t = d.previousElementSibling;
          if (t) t.setAttribute('aria-expanded', 'false');
          const w = d.closest('.custom-select-wrap');
          if (w) w.classList.remove('open');
        });
      }
    });
  }
}

// 开放给外部或未来扩展分析模块的标准化 API 接口
window.CollectorMusicAnalytics = {
  getState() {
    return state;
  },
  getAnalysisRecords() {
    return analysisRecords();
  },
  getAnalysisData() {
    return state.analysis;
  },
  refresh() {
    return renderAnalytics();
  },
  registerSlotComponent(renderFn) {
    const slot = $('analytics-custom-slot');
    if (slot && typeof renderFn === 'function') {
      try {
        slot.hidden = false;
        renderFn(slot, state.data);
      } catch (err) {
        console.error('Failed to mount custom analytics slot:', err);
      }
    }
  }
};

/* ==========================================================================
   事件监听器绑定 EVENT LISTENERS
   ========================================================================== */
$('refresh').addEventListener('click', () => load(true));
$('search').addEventListener('input', () => {
  state.page = 0;
  renderRecords();
});
$('record-source').addEventListener('change', () => {
  state.page = 0;
  renderRecords();
});
for (const id of ['record-period', 'record-sort', 'record-size']) {
  $(id).addEventListener('change', () => {
    state.page = 0;
    renderRecords();
  });
}
$('record-reset').addEventListener('click', () => {
  resetRecords();
  $('search').focus({preventScroll: true});
});
$('record-page-form').addEventListener('submit', event => {
  event.preventDefault();
  state.page = Number($('record-page').value) - 1;
  renderRecords();
  $('records-title').scrollIntoView({block: 'start'});
});
$('collection-page-form').addEventListener('submit', event => {
  event.preventDefault();
  state.collectionPage = Number($('collection-page').value) - 1;
  renderCollection();
  $('collection-panel').scrollIntoView({block: 'start'});
});
document.querySelectorAll('[data-record-shortcut]').forEach(link => link.addEventListener('click', () => {
  resetRecords(link.dataset.recordShortcut);
  requestAnimationFrame(() => $('search').focus({preventScroll: true}));
}));
$('search-history').addEventListener('click', () => requestAnimationFrame(() => $('search').focus({preventScroll: true})));
$('prev').addEventListener('click', () => {
  state.page--;
  renderRecords();
});
$('next').addEventListener('click', () => {
  state.page++;
  renderRecords();
});
$('period').addEventListener('change', renderAnalytics);

document.querySelectorAll('[data-chart-source]').forEach(button => button.addEventListener('click', () => {
  state.chartSource = button.dataset.chartSource;
  document.querySelectorAll('[data-chart-source]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
  renderAnalytics();
}));

$('ranking-scope').addEventListener('change', () => {
  state.rankingLimit = 8;
  renderRankings();
});
$('ranking-more').addEventListener('click', () => {
  state.rankingLimit += 10;
  renderRankings();
});

function selectCollection(collection, resetKind = false) {
  state.collection = collection;
  state.collectionPage = 0;
  $('collection-search').value = '';
  if (resetKind) setSelect('collection-kind', 'all');
  document.querySelectorAll('[data-collection]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.collection === collection)));
  renderCollection();
}
document.querySelectorAll('[data-collection]').forEach(button => button.addEventListener('click', () => {
  selectCollection(button.dataset.collection);
}));
document.querySelectorAll('[data-collection-shortcut]').forEach(link => link.addEventListener('click', () => {
  selectCollection(link.dataset.collectionShortcut, true);
  requestAnimationFrame(() => $('collection-search').focus({preventScroll: true}));
}));
$('collection-reset').addEventListener('click', () => {
  selectCollection(state.collection, true);
  $('collection-search').focus({preventScroll: true});
});

$('collection-search').addEventListener('input', () => {
  state.collectionPage = 0;
  renderCollection();
});
$('collection-kind').addEventListener('change', () => {
  state.collectionPage = 0;
  renderCollection();
});
$('collection-prev').addEventListener('click', () => {
  state.collectionPage--;
  renderCollection();
});
$('collection-next').addEventListener('click', () => {
  state.collectionPage++;
  renderCollection();
});

$('help').addEventListener('click', () => $('help-dialog').showModal());
$('update-guide').addEventListener('click', () => $('help-dialog').showModal());
for (const id of ['close-help', 'help-done']) {
  const el = $(id);
  if (el) el.addEventListener('click', () => $('help-dialog').close());
}

const navigationSections = ['overview', 'records', 'analytics', 'platform-reports', 'library', 'sources'].map($);
const masthead = document.querySelector('.masthead');
function updateNavigation() {
  const offset = masthead.getBoundingClientRect().height + 64;
  const current = navigationSections.filter(section => section.getBoundingClientRect().top <= offset).at(-1) || navigationSections[0];
  document.querySelectorAll('.masthead nav a').forEach(link => {
    const active = link.hash === '#' + current.id;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  });
}
let navigationFrame;
document.addEventListener('scroll', () => {
  if (navigationFrame) return;
  navigationFrame = requestAnimationFrame(() => { navigationFrame = null; updateNavigation(); });
}, {passive: true});
new ResizeObserver(() => {
  document.documentElement.style.scrollPaddingTop = (masthead.offsetHeight + 16) + 'px';
  updateNavigation();
}).observe(masthead);
updateNavigation();

setupAnalyticsTabs();
enhanceSelects();
load();
