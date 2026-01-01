'use strict';

// Platform aggregates keep their own periods, provenance and empty-state semantics.
window.CollectorMusicReports = (() => {
  const byId = id => document.getElementById(id);
  const fmt = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 1});
  let reports = null, selected = '', selectedPeriod = '', timezone = 'Asia/Shanghai';
  const element = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  };
  const validNumber = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const formatDate = value => {
    if (!value) return '时间未提供';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '时间未提供' : new Intl.DateTimeFormat('zh-CN', {
      timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).format(date);
  };
  const isRealtime = entry => entry === reports?.realtime;
  const periodKey = entry => `${entry.kind}:${entry.period_label || entry.period_start || ''}`;
  const entries = () => [reports?.realtime, ...(reports?.monthly || [])].filter(Boolean);
  const monthLabel = entry => `${entry.period_label || '月份未提供'} · ${isRealtime(entry) ? '实时快照' : '完整月报'}`;
  const metric = (label, value, detail = '') => {
    const el = element('div', 'report-metric');
    el.append(element('span', 'report-metric-label', label), element('strong', 'report-metric-value', value));
    if (detail) el.append(element('span', 'report-metric-detail', detail));
    return el;
  };

  function breakdown(parent, title, items, unit = '%') {
    if (!items?.length) return;
    const section = element('div', 'report-breakdown');
    if (title) section.append(element('h3', '', title));
    const list = element('dl', 'report-list');
    for (const item of items) {
      const row = element('div', 'report-list-row');
      const value = unit === '%' ? item.percent : item.count;
      row.append(element('dt', '', item.name || '名称未提供'), element('dd', '', validNumber(value) ? `${fmt.format(value)}${unit}` : '—'));
      list.append(row);
    }
    section.append(list);
    parent.append(section);
  }

  function renderDaily(entry) {
    const container = byId('report-daily');
    container.replaceChildren();
    byId('report-daily-range').replaceChildren();
    byId('report-daily-detail').textContent = '';
    const daily = entry.daily || [];
    const available = daily.filter(row => validNumber(row.duration_minutes));
    byId('report-daily-section').hidden = !available.length;
    if (!available.length) return;
    const max = Math.max(1, ...available.map(row => row.duration_minutes));
    for (const [index, row] of daily.entries()) {
      const known = validNumber(row.duration_minutes);
      const message = `${row.date}：${known ? `${fmt.format(row.duration_minutes)} 分钟` : '时长未知'}`;
      const button = element('button', 'report-day' + (known ? '' : ' unknown'));
      button.type = 'button';
      button.title = message;
      button.setAttribute('aria-label', message);
      button.tabIndex = index === 0 ? 0 : -1;
      const fill = element('span', 'report-day-fill');
      fill.style.height = known ? `${Math.max(1, row.duration_minutes / max * 100)}%` : '1%';
      button.append(fill);
      const show = () => { byId('report-daily-detail').textContent = message; };
      button.addEventListener('focus', show);
      button.addEventListener('pointerenter', show);
      button.addEventListener('click', show);
      button.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const target = event.key === 'Home' ? 0 : event.key === 'End' ? daily.length - 1
          : Math.max(0, Math.min(daily.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)));
        container.querySelectorAll('button').forEach((el, i) => { el.tabIndex = i === target ? 0 : -1; });
        container.children[target]?.focus();
      });
      container.append(button);
    }
    byId('report-daily-range').append(element('span', '', daily[0].date), element('span', '', daily.at(-1).date));
  }

  function renderSelected() {
    const entry = entries().find(item => item.id === selected);
    const status = byId('reports-status');
    const content = byId('reports-content');
    byId('report-metrics').replaceChildren();
    byId('report-breakdowns').replaceChildren();
    byId('report-daily-section').hidden = true;
    byId('report-source-note').textContent = '';
    const empty = entry?.status === 'empty' || entry?.data_status === 'empty';
    content.hidden = !entry || empty;
    if (!entry) {
      const failed = reports?.issues?.length || reports?.status === 'unavailable';
      status.textContent = failed ? '平台报告采集未完成，暂无可展示的成功快照。' : '尚未采集平台报告。';
      return;
    }
    if (empty) {
      status.textContent = entry.status === 'stale' ? '上次成功采集为空报告；最近采集失败，暂无可展示的数据。'
        : '平台本次返回空报告，未沿用更早的数据。';
      return;
    }
    const range = [entry.period_start, entry.period_end].filter(Boolean).join(' — ');
    status.textContent = `${range}${entry.status === 'stale' ? ' · 最近采集失败，保留上次成功快照' : ''}`;
    const metrics = entry.metrics || {};
    const summary = byId('report-metrics');
    if (validNumber(metrics.duration_minutes)) summary.append(metric('平台收听时长', `${fmt.format(metrics.duration_minutes)} 分钟`));
    if (validNumber(metrics.listen_days)) summary.append(metric('有收听的日期', `${fmt.format(metrics.listen_days)} 天`));
    if (entry.top_song?.name) summary.append(metric('最常听歌曲', entry.top_song.name, entry.top_song.artist || ''));
    if (entry.top_artist?.name) summary.append(metric('最常听艺人', entry.top_artist.name));
    summary.hidden = !summary.children.length;
    renderDaily(entry);
    const breakdowns = byId('report-breakdowns');
    breakdown(breakdowns, '曲风', entry.genres);
    breakdown(breakdowns, '语种', entry.languages);
    breakdown(breakdowns, '年代', entry.ages, ' 首');
    const notes = [`采集于 ${formatDate(entry.observed_at)}`];
    if (isRealtime(entry)) notes.push('实时快照，非完整月份');
    if (!validNumber(metrics.duration_minutes) && metrics.duration_raw != null) notes.push('时长单位待核实，暂不换算展示');
    byId('report-source-note').textContent = notes.join(' · ');
  }

  function renderPreference() {
    const entry = reports?.preference;
    const container = byId('report-preference-tags');
    container.replaceChildren();
    byId('report-preference').hidden = !entry;
    if (!entry) return;
    const empty = entry.status === 'empty' || entry.data_status === 'empty' || !entry.tags?.length;
    byId('report-preference-date').textContent = `${empty ? '平台本次未返回偏好标签。' : ''}采集于 ${formatDate(entry.observed_at)}`
      + (entry.status === 'stale' ? ' · 最近采集失败，保留上次成功快照' : '');
    if (!empty) breakdown(container, '', entry.tags);
  }

  function render(data, zone) {
    reports = data;
    timezone = zone || 'Asia/Shanghai';
    const select = byId('report-period');
    const choices = entries();
    if (!choices.some(entry => entry.id === selected)) {
      selected = (choices.find(entry => periodKey(entry) === selectedPeriod) || reports?.monthly?.[0] || reports?.realtime)?.id || '';
    }
    selectedPeriod = choices.find(entry => entry.id === selected) ? periodKey(choices.find(entry => entry.id === selected)) : '';
    select.replaceChildren();
    for (const entry of choices) {
      const option = element('option', '', monthLabel(entry));
      option.value = entry.id;
      select.append(option);
    }
    if (!choices.length) {
      const option = element('option', '', '尚无报告');
      option.value = '';
      select.append(option);
    }
    select.value = selected;
    select.disabled = !choices.length;
    select.dispatchEvent(new Event('selectsync'));
    const names = {monthly: '完整月报', realtime: '实时报告', preference: '曲风偏好'};
    const issues = (reports?.issues || []).map(issue => typeof issue === 'string' ? issue
      : issue.status === 'unavailable' && names[issue.kind] ? `${names[issue.kind]}尚无成功快照。` : '').filter(Boolean);
    byId('reports-issues').textContent = issues.join(' ');
    byId('reports-issues').hidden = !issues.length;
    renderSelected();
    renderPreference();
  }

  document.addEventListener('DOMContentLoaded', () => {
    byId('report-period').addEventListener('change', event => {
      selected = event.target.value;
      const entry = entries().find(item => item.id === selected);
      selectedPeriod = entry ? periodKey(entry) : '';
      renderSelected();
    });
  });
  return {render};
})();
