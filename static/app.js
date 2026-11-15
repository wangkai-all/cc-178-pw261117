/* Incremental regional roll-up - browser client.
 *
 * Plain ES2020, no framework, no build step.  All aggregation happens on the
 * server; this file renders state, collects edits into a pending batch, and
 * never computes totals on its own (so what you see is what was committed).
 */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  data: null,
  scenarios: [],
  pending: [],
  impact: null,
  audit: null,
  chartTable: false,
  summaryCompare: false,
  batchDetail: null,
};

const ZONE_COLORS = ['--series-1', '--series-2', '--series-3', '--series-4',
                     '--series-5', '--series-6', '--series-7', '--series-8'];

const MEASURES = [
  ['order_count', '订单数'],
  ['amount_count', '非空金额笔数'],
  ['null_count', '空金额笔数'],
  ['zero_count', '零金额笔数'],
  ['total_cents', '金额合计'],
  ['unit_price_cents', '客单价'],
];

// ---------------------------------------------------------------- utilities

function money(cents) {
  if (cents === null || cents === undefined) return null;
  const neg = cents < 0;
  const abs = Math.abs(cents);
  return (neg ? '-' : '') + Math.floor(abs / 100).toLocaleString('en-US') +
    '.' + String(abs % 100).padStart(2, '0');
}

function num(value) {
  if (value === null || value === undefined) return null;
  return Number(value).toLocaleString('en-US');
}

/** Typed DOM builder: text goes in as text, never innerHTML. */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style' && typeof value === 'object') {
      for (const [prop, val] of Object.entries(value)) {
        if (prop.startsWith('--')) node.style.setProperty(prop, val);
        else node.style[prop] = val;
      }
    }
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function toast(message, kind = '') {
  const box = $('#toast');
  box.textContent = message;
  box.className = 'toast' + (kind ? ' ' + kind : '');
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { box.hidden = true; }, 4800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let body = null;
  try { body = await response.json(); } catch (_) { /* non-JSON */ }
  if (!response.ok) {
    const message = body?.error?.message || `请求失败 (${response.status})`;
    throw new Error(message);
  }
  return body;
}

function zoneColor(zoneKey) {
  if (zoneKey === '__unmatched__') return 'var(--unmatched)';
  const index = (state.data?.zones || []).findIndex((z) => z.zone_id === zoneKey);
  return `var(${ZONE_COLORS[(index < 0 ? 0 : index) % ZONE_COLORS.length]})`;
}

function zoneName(key) {
  if (key === '__unmatched__') return '未匹配门店 / 未分配区域';
  return (state.data?.zones || []).find((z) => z.zone_id === key)?.name || key;
}

function fmtMeasure(measure, value) {
  if (value === null || value === undefined) {
    return measure === 'unit_price_cents' ? '未定义' : '—';
  }
  if (measure === 'total_cents' || measure === 'unit_price_cents') return money(value);
  return num(value);
}

// ------------------------------------------------------------------- theme

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('theme', theme);
  $('#theme-toggle').textContent = theme === 'dark' ? '浅色' : '深色';
  if (state.data) renderSummaryChart();
}

$('#theme-toggle').addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});

// ------------------------------------------------------------------- tabs

function showTab(name) {
  $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
  $$('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
  if (name === 'impact' && !state.impact) loadImpact();
}

$$('.tab').forEach((tab) => tab.addEventListener('click', () => showTab(tab.dataset.tab)));

// ------------------------------------------------------------------- state

async function loadState() {
  state.data = await api('/api/state');
  renderAll();
}

function renderAll() {
  renderHeader();
  renderSummary();
  renderSource();
  renderPending();
  renderBatchHistory();
  renderAttempts();
  renderImpactSelectors();
  renderAuditSelectors();
}

function renderHeader() {
  const data = state.data;
  const latest = data.snapshot_batch;
  $('#pill-snapshot').textContent = data.snapshot_version
    ? `快照 v${data.snapshot_version} · ${latest?.batch_id || ''}`
    : '快照 —';
  const allMatch = data.reconciliation.every((r) => r.is_match);
  const pill = $('#pill-store');
  pill.textContent = data.snapshot_version
    ? (allMatch ? '持久化 · 增量=全量' : '持久化 · 存在差异')
    : '持久化 · 空';
  pill.className = 'pill ' + (!data.snapshot_version ? '' : allMatch ? 'ok' : 'bad');
}

// ----------------------------------------------------------------- summary

function summaryRow(zoneKey) {
  return (state.data.summary || []).find((r) => r.zone_key === zoneKey) || null;
}

function renderSummary() {
  const data = state.data;
  const summary = data.summary || [];
  const fullByZone = Object.fromEntries((data.full || []).map((r) => [r.zone_key, r]));
  const reconByZone = {};
  for (const item of data.reconciliation || []) {
    const bucket = reconByZone[item.zone_key] || (reconByZone[item.zone_key] = []);
    bucket.push(item);
  }

  const matched = summary.filter((r) => r.zone_key !== '__unmatched__');
  const unmatched = summary.find((r) => r.zone_key === '__unmatched__');
  const orders = matched.reduce((sum, r) => sum + r.order_count, 0);
  const amountCount = matched.reduce((sum, r) => sum + r.amount_count, 0);
  const total = matched.reduce((sum, r) => sum + r.total_cents, 0);
  const unit = amountCount ? Math.round(total / amountCount) : null;
  const mismatches = (data.reconciliation || []).filter((r) => !r.is_match);

  // Banner: what state are we in?
  const banner = $('#summary-banner');
  banner.replaceChildren();
  if (!data.snapshot_version) {
    banner.append(el('div', { class: 'banner warn' },
      el('span', {}, '还没有任何已提交的批次。源表仍可编辑，提交第一个批次后会生成快照。')));
  } else if (mismatches.length) {
    banner.append(el('div', { class: 'banner bad' },
      el('span', {}, `增量汇总与按同一快照完整重算的结果有 ${mismatches.length} 项不一致，请到「对照与审计」查看。`)));
  } else if (summary.length === 0) {
    banner.append(el('div', { class: 'banner warn' },
      el('span', {}, '快照存在但没有任何分组：所有销售都已撤回，源表里也没有有效销售。')));
  }

  const stats = $('#summary-stats');
  stats.replaceChildren(
    el('div', { class: 'stat hero' },
      el('div', { class: 'stat-label' }, '已匹配区域金额合计'),
      el('div', { class: 'stat-value' }, money(total) ?? '0.00'),
      el('div', { class: 'stat-sub' }, `不含未匹配分组${unmatched ? `（${money(unmatched.total_cents)}）` : ''}`)),
    el('div', { class: 'stat' },
      el('div', { class: 'stat-label' }, '分组数'),
      el('div', { class: 'stat-value' }, String(summary.length)),
      el('div', { class: 'stat-sub' }, `${matched.length} 个区域${unmatched ? ' + 1 个未匹配' : ''}`)),
    el('div', { class: 'stat' },
      el('div', { class: 'stat-label' }, '订单数'),
      el('div', { class: 'stat-value' }, num(orders) ?? '0'),
      el('div', { class: 'stat-sub' }, '含空金额与零金额销售')),
    el('div', { class: 'stat' },
      el('div', { class: 'stat-label' }, '非空金额笔数'),
      el('div', { class: 'stat-value' }, num(amountCount) ?? '0'),
      el('div', { class: 'stat-sub' }, `空金额 ${num(matched.reduce((s, r) => s + r.null_count, 0)) ?? 0} 笔`)),
    el('div', { class: 'stat' },
      el('div', { class: 'stat-label' }, '区域客单价'),
      el('div', { class: 'stat-value' }, unit === null ? '未定义' : money(unit)),
      el('div', { class: 'stat-sub' }, '金额合计 ÷ 非空金额笔数')),
  );

  // Per-zone states: no-group vs zero-sum.
  const zoneStates = $('#zone-states');
  zoneStates.replaceChildren();
  const zones = data.zones || [];
  for (const zone of zones) {
    const row = summaryRow(zone.zone_id);
    zoneStates.append(zoneCard(zone.zone_id, zone.name, row));
  }
  if (unmatched) zoneStates.append(zoneCard('__unmatched__', zoneName('__unmatched__'), unmatched));

  // Chart
  renderSummaryChart();

  // Table
  const tableWrap = $('#summary-table');
  const showFull = state.summaryCompare;
  const header = ['区域', '订单数', '非空笔数', '空金额', '零金额', '金额合计', '客单价'];
  if (showFull) header.push('全量重算一致');
  const table = el('table', {},
    el('thead', {}, el('tr', {}, header.map((h, i) =>
      el('th', { class: i === 0 ? '' : 'num' }, h)))),
  );
  const body = el('tbody');
  if (!summary.length) {
    body.append(el('tr', {}, el('td', { colspan: header.length, class: 'muted' },
      '没有任何分组。所有销售都被撤回后，这里不会保留一行零值。')));
  }
  for (const row of summary) {
    const tr = el('tr', {},
      el('td', {},
        el('span', { class: 'dot', style: { background: zoneColor(row.zone_key), marginRight: '8px' } }),
        row.zone_name),
      el('td', { class: 'num' }, num(row.order_count)),
      el('td', { class: 'num' }, num(row.amount_count)),
      el('td', { class: 'num' }, num(row.null_count)),
      el('td', { class: 'num' }, num(row.zero_count)),
      el('td', { class: 'num' }, money(row.total_cents)),
      el('td', { class: row.unit_price_cents === null ? 'num missing' : 'num' },
        fmtMeasure('unit_price_cents', row.unit_price_cents)),
    );
    if (showFull) {
      const findings = reconByZone[row.zone_key] || [];
      const ok = findings.every((f) => f.is_match);
      tr.append(el('td', {}, el('span', { class: 'tag ' + (ok ? 'good' : 'bad') },
        ok ? '一致' : '有差异')));
    }
    body.append(tr);
  }
  // Regions with no group at all are explicit, not silently missing.
  for (const zone of zones) {
    if (summaryRow(zone.zone_id)) continue;
    const tr = el('tr', {},
      el('td', {},
        el('span', { class: 'dot', style: { background: zoneColor(zone.zone_id), marginRight: '8px' } }),
        el('span', { class: 'muted' }, zone.name)),
      el('td', { colspan: header.length - 1, class: 'muted' },
        '无分组：该区域没有任何销售，不参与任何合计（与「合计为零」不同）'),
    );
    body.append(tr);
  }
  table.append(body);
  tableWrap.replaceChildren(table);

  const zeroSum = matched.filter((r) => r.total_cents === 0);
  const zeroUndefined = zeroSum.filter((r) => r.unit_price_cents === null);
  $('#summary-footnote').textContent = [
    `快照 v${data.snapshot_version ?? '—'}，来自批次 ${data.snapshot_batch?.batch_id ?? '—'}`,
    zeroSum.length
      ? `合计为零但仍有分组的区域：${zeroSum.map((r) => r.zone_name).join('、')}` +
        (zeroUndefined.length
          ? `（其中 ${zeroUndefined.map((r) => r.zone_name).join('、')} 没有非空金额，客单价为「未定义」）`
          : '（这些分组仍有金额为零的销售，所以客单价是 0.00，不是「未定义」）')
      : '当前没有「存在分组但合计为零」的区域',
    '客单价 = 金额合计 ÷ 非空金额笔数，按整数分四舍五入。',
  ].join(' · ');
}

function zoneCard(zoneKey, name, row) {
  if (!row) {
    return el('div', { class: 'zone-card no-group', style: { '--zone-color': zoneColor(zoneKey) } },
      el('div', { class: 'zc-name' },
        el('span', { class: 'dot', style: { background: zoneColor(zoneKey) } }), name),
      el('div', { class: 'zc-state' }, '无分组'),
      el('div', { class: 'zc-flag' }, '没有任何销售，不出现在合计中'));
  }
  const zeroSum = row.total_cents === 0;
  return el('div', { class: 'zone-card' + (zeroSum ? ' zero-sum' : ''), style: { '--zone-color': zoneColor(zoneKey) } },
    el('div', { class: 'zc-name' },
      el('span', { class: 'dot', style: { background: zoneColor(zoneKey) } }), row.zone_name),
    el('div', { class: 'zc-state' },
      `订单 ${num(row.order_count)} · 非空 ${num(row.amount_count)} · 空 ${num(row.null_count)} · 零 ${num(row.zero_count)}`),
    el('div', { class: 'zc-amount' }, money(row.total_cents)),
    el('div', { class: 'zc-flag' },
      zeroSum
        ? (row.unit_price_cents === null
            ? '合计为零：仍有分组，但没有非空金额，客单价未定义'
            : `合计为零：仍有分组，客单价 ${money(row.unit_price_cents)}`)
        : `客单价 ${money(row.unit_price_cents)}`));
}

function renderSummaryChart() {
  const wrap = $('#chart-wrap');
  if (!wrap) return;
  // Groups with no sales at all are listed as explicit zero rows at the end, so
  // "no group" is never rendered as a zero-length bar indistinguishable from a
  // group whose totals happen to be zero.
  const missingZones = (state.data.zones || [])
    .filter((zone) => !summaryRow(zone.zone_id) && !(state.data.summary || []).some(
      (r) => r.zone_key === zone.zone_id))
    .map((zone) => ({ zone_key: zone.zone_id, zone_name: zone.name, absent: true }));
  const rows = (state.data.summary || []).slice().sort((a, b) => b.total_cents - a.total_cents);
  if (!rows.length) {
    wrap.replaceChildren(el('p', { class: 'empty-state' },
      '没有分组可画。所有销售都被撤回时，汇总不会保留一行零值。'));
    return;
  }
  const max = Math.max(1, ...rows.map((r) => r.total_cents));
  const chart = el('div', { class: 'bar-chart' });

  for (const row of rows) {
    const pct = max ? (row.total_cents / max) * 100 : 0;
    const rowEl = el('div', {
      class: 'bar-row' + (row.total_cents === 0 ? ' empty' : ''),
      tabindex: '0',
      role: 'img',
      'aria-label': `${row.zone_name}：金额合计 ${money(row.total_cents)}，订单数 ${row.order_count}`,
    },
      el('div', { class: 'bar-label' },
        el('span', { class: 'dot', style: { background: zoneColor(row.zone_key) } }),
        row.zone_name),
      el('div', { class: 'bar-track' },
        el('div', {
          class: 'bar-fill' + (row.total_cents === 0 ? ' zero' : ''),
          style: { width: Math.max(pct, row.total_cents === 0 ? 0 : 1.5) + '%', background: zoneColor(row.zone_key) },
        })),
      el('div', { class: 'bar-value' }, row.total_cents === 0 ? '0.00（合计为零）' : money(row.total_cents)),
    );
    const showTip = (event) => showChartTooltip(event, row);
    rowEl.addEventListener('pointermove', showTip);
    rowEl.addEventListener('focus', showTip);
    rowEl.addEventListener('pointerleave', hideChartTooltip);
    rowEl.addEventListener('blur', hideChartTooltip);
    chart.append(rowEl);
  }
  for (const zone of missingZones) {
    chart.append(el('div', {
      class: 'bar-row empty',
      style: { '--zone-color': zoneColor(zone.zone_key) },
      tabindex: '0',
      'aria-label': `${zone.zone_name}：无分组，没有任何销售`,
    },
      el('div', { class: 'bar-label' },
        el('span', { class: 'dot', style: { background: zoneColor(zone.zone_key), opacity: 0.4 } }),
        zone.zone_name),
      el('div', { class: 'bar-track' },
        el('div', { class: 'bar-fill dash', style: { '--zone-color': zoneColor(zone.zone_key) } })),
      el('div', { class: 'bar-value' }, '无分组'),
    ));
  }
  chart.append(el('p', { class: 'axis-note' },
    '横条长度 = 金额合计。订单数与非空金额笔数不参与条长，避免两个量共用一个刻度。虚线框 = 该区域根本没有分组。'));
  wrap.replaceChildren(chart);

  if (state.chartTable) {
    const table = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, '区域'), el('th', { class: 'num' }, '订单数'),
        el('th', { class: 'num' }, '非空笔数'), el('th', { class: 'num' }, '金额合计'),
        el('th', { class: 'num' }, '客单价'), el('th', {}, '状态'))),
      el('tbody', {}, [
        ...rows.map((r) => el('tr', {},
          el('td', {}, r.zone_name),
          el('td', { class: 'num' }, num(r.order_count)),
          el('td', { class: 'num' }, num(r.amount_count)),
          el('td', { class: 'num' }, money(r.total_cents)),
          el('td', { class: r.unit_price_cents === null ? 'num missing' : 'num' },
            fmtMeasure('unit_price_cents', r.unit_price_cents)),
          el('td', {}, r.total_cents === 0 ? '合计为零' : '—'))),
        ...missingZones.map((z) => el('tr', {},
          el('td', { class: 'muted' }, z.zone_name),
          el('td', { class: 'num muted' }, '—'),
          el('td', { class: 'num muted' }, '—'),
          el('td', { class: 'num muted' }, '—'),
          el('td', { class: 'num muted' }, '—'),
          el('td', { class: 'muted' }, '无分组'))),
      ]));
    const holder = $('#chart-table');
    holder.replaceChildren(table);
    holder.hidden = false;
  } else {
    $('#chart-table').hidden = true;
  }
}

let tooltipEl = null;
function showChartTooltip(event, row) {
  hideChartTooltip();
  tooltipEl = el('div', { class: 'chart-tooltip' },
    el('div', { class: 'tt-name' },
      el('span', { class: 'dot', style: { background: zoneColor(row.zone_key) } }), row.zone_name),
    el('div', { class: 'tt-value' }, money(row.total_cents)),
    ...MEASURES.filter(([key]) => key !== 'total_cents').map(([key, label]) =>
      el('div', { class: 'tt-line' }, el('span', {}, label), el('span', {}, fmtMeasure(key, row[key])))),
  );
  document.body.append(tooltipEl);
  const rect = event.currentTarget.getBoundingClientRect();
  const tooltipRect = tooltipEl.getBoundingClientRect();
  let left = rect.right - tooltipRect.width - 8;
  let top = rect.bottom + 6;
  if (top + tooltipRect.height > window.innerHeight - 8) top = rect.top - tooltipRect.height - 6;
  tooltipEl.style.left = Math.max(8, left) + 'px';
  tooltipEl.style.top = Math.max(8, top) + 'px';
}
function hideChartTooltip() {
  if (tooltipEl) { tooltipEl.remove(); tooltipEl = null; }
}

$('#chart-view-toggle').addEventListener('click', () => {
  state.chartTable = !state.chartTable;
  $('#chart-view-toggle').textContent = state.chartTable ? '图表视图' : '表格视图';
  renderSummaryChart();
});
$('#summary-compare-toggle').addEventListener('change', (event) => {
  state.summaryCompare = event.target.checked;
  renderSummary();
});

// ------------------------------------------------------------------ source

function renderSource() {
  const data = state.data;
  const storeSelect = $('#new-sale-store');
  const current = storeSelect.value;
  storeSelect.replaceChildren(
    el('option', { value: '' }, '（无门店）'),
    ...(data.stores || []).map((store) => el('option', { value: store.store_id },
      `${store.store_id} · ${store.name}${store.zone_name ? '（' + store.zone_name + '）' : '（未分配）'}`)),
    el('option', { value: 'S-999' }, 'S-999 · 不存在的门店（示例未匹配）'),
  );
  storeSelect.value = current || '';

  const query = ($('#sale-search').value || '').trim().toLowerCase();
  const showDeleted = $('#show-deleted').checked;
  const rows = (data.sales || []).filter((sale) => {
    if (!showDeleted && sale.status !== 'active') return false;
    if (!query) return true;
    return [sale.sale_id, sale.store_id, sale.store_name, sale.zone_name, sale.status]
      .filter(Boolean).some((value) => String(value).toLowerCase().includes(query));
  });

  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '销售主键'), el('th', {}, '门店'), el('th', {}, '区域'),
      el('th', {}, '状态'), el('th', {}, '来源批次'), el('th', {}, '金额 / 门店调整'))),
  );
  const body = el('tbody');
  if (!rows.length) {
    body.append(el('tr', {}, el('td', { colspan: 6, class: 'muted' }, '没有匹配的销售。')));
  }
  for (const sale of rows) {
    const deleted = sale.status !== 'active';
    const pendingOp = state.pending.find(
      (op) => (op.type === 'update' || op.type === 'delete') && op.sale_id === sale.sale_id);
    body.append(el('tr', { class: pendingOp ? 'row-pending' : '' },
      el('td', { class: 'mono' }, sale.sale_id),
      el('td', {},
        sale.store_id
          ? `${sale.store_name || '未知门店'} (${sale.store_id})`
          : el('span', { class: 'muted' }, '（无门店）')),
      el('td', {},
        sale.zone_name
          ? sale.zone_name
          : el('span', { class: 'tag warn' }, '未匹配')),
      el('td', {}, deleted ? el('span', { class: 'tag' }, 'deleted')
                           : el('span', { class: 'tag good' }, 'active')),
      el('td', { class: 'mono' }, sale.updated_batch || '—'),
      el('td', {},
        deleted
          ? el('span', { class: 'muted' }, '—')
          : el('div', { class: 'row-edit' },
            el('input', {
              type: 'text',
              value: pendingOp && 'amount' in pendingOp ? (pendingOp.amount ?? '') : (sale.amount ?? ''),
              placeholder: '空值',
              'aria-label': `${sale.sale_id} 金额`,
              onchange: (event) => queueUpdate(sale, { amount: event.target.value }),
            }),
            el('select', {
              'aria-label': `${sale.sale_id} 门店`,
              onchange: (event) => queueUpdate(sale, { store_id: event.target.value || null }),
            },
              el('option', { value: '' }, '（无门店）'),
              ...(data.stores || []).map((store) => el('option',
                {
                  value: store.store_id,
                  selected: store.store_id ===
                    (pendingOp && 'store_id' in pendingOp ? pendingOp.store_id : sale.store_id),
                },
                `${store.store_id} · ${store.name}`)),
            ),
              el('button', {
                class: 'btn-mini danger', type: 'button',
                onclick: () => queueDelete(sale),
              }, '删除'),
            )),
    ));
  }
  table.append(body);
  $('#sales-table').replaceChildren(table);

  // Stores
  const storeTable = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '门店主键'), el('th', {}, '名称'), el('th', {}, '区域'),
      el('th', { class: 'num' }, '有效销售'), el('th', {}, '操作'))),
  );
  const storeBody = el('tbody');
  for (const store of data.stores || []) {
    storeBody.append(el('tr', {},
      el('td', { class: 'mono' }, store.store_id),
      el('td', {}, store.name),
      el('td', {}, store.zone_name || el('span', { class: 'tag warn' }, '未分配')),
      el('td', { class: 'num' }, num(store.active_sales)),
      el('td', {},
        el('div', { class: 'row-edit' },
          el('select', {
            'aria-label': `${store.store_id} 区域`,
            onchange: (event) => queueMove(store, event.target.value || null),
          },
            el('option', { value: '' }, '（未分配）'),
            ...(data.zones || []).map((zone) => el('option',
              { value: zone.zone_id, selected: zone.zone_id === store.zone_id ? true : null },
              zone.name)),
          ),
          el('span', { class: 'one-shot' }, '改变即生成整店迁移'))),
    ));
  }
  storeTable.append(storeBody);
  $('#stores-table').replaceChildren(storeTable);
}

$('#sale-search').addEventListener('input', renderSource);
$('#show-deleted').addEventListener('change', renderSource);

$('#new-sale-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const saleId = String(form.get('sale_id') || '').trim();
  const storeId = String(form.get('store_id') || '').trim();
  const amount = form.get('amount');
  if (!saleId) return;
  if (state.pending.some((op) => op.type === 'insert' && op.sale_id === saleId)) {
    showNewSaleHint(`待提交里已经有 ${saleId} 的新增`, true);
    return;
  }
  state.pending.push({
    type: 'insert', sale_id: saleId,
    store_id: storeId || null,
    amount: amount === '' ? null : amount,
  });
  event.target.reset();
  $('#new-sale-store').value = '';
  showNewSaleHint(`已加入待提交：${saleId}（${amount === '' ? '空金额' : amount === '0' ? '零金额' : '金额 ' + amount}）`);
  renderPending();
});

function showNewSaleHint(text, bad = false) {
  const hint = $('#new-sale-hint');
  hint.textContent = text;
  hint.style.color = bad ? 'var(--critical)' : 'var(--ink-3)';
}

function queueUpdate(sale, changes) {
  const op = { type: 'update', sale_id: sale.sale_id };
  let changed = false;
  if ('amount' in changes) {
    const next = changes.amount === '' ? null : changes.amount;
    if (next !== sale.amount) { op.amount = next; changed = true; }
  }
  if ('store_id' in changes && changes.store_id !== sale.store_id) {
    op.store_id = changes.store_id;
    changed = true;
  }
  if (!changed) return;
  // Merge into the newest pending update for the same sale so a field edited
  // twice in one session produces one update, not a chain of no-op updates.
  for (let index = state.pending.length - 1; index >= 0; index -= 1) {
    const prior = state.pending[index];
    if (prior.type === 'update' && prior.sale_id === sale.sale_id) {
      Object.assign(prior, op);
      renderPending();
      const bits = [];
      if ('amount' in prior) bits.push(`金额 → ${prior.amount === null ? '空值' : prior.amount}`);
      if ('store_id' in prior) bits.push(`门店 → ${prior.store_id || '无门店'}`);
      toast(`已合并到待提交的 ${sale.sale_id}：${bits.join('，')}`);
      return;
    }
    if (prior.type !== 'update' || prior.sale_id !== sale.sale_id) break;
  }
  state.pending.push(op);
  renderPending();
  toast(`已加入待提交：更新 ${sale.sale_id}（提交时会先撤回旧贡献再加入新贡献）`);
}

function queueDelete(sale) {
  state.pending.push({ type: 'delete', sale_id: sale.sale_id });
  renderPending();
  toast(`已加入待提交：删除 ${sale.sale_id}`);
}

function queueMove(store, zoneId) {
  if (zoneId === store.zone_id) return;
  state.pending.push({ type: 'move_store', store_id: store.store_id, zone_id: zoneId });
  renderPending();
  toast(`已加入待提交：${store.name} 迁往 ${zoneId ? zoneName(zoneId) : '未分配'}`);
  renderSource();
}

function describeOp(op, index) {
  switch (op.type) {
    case 'insert':
      return `新增 ${op.sale_id} → ${op.store_id || '无门店'}，金额 ${op.amount === null ? '空值' : op.amount}`;
    case 'update': {
      const bits = [];
      if ('amount' in op) bits.push(`金额 → ${op.amount === null ? '空值' : op.amount}`);
      if ('store_id' in op) bits.push(`门店 → ${op.store_id || '无门店'}`);
      return `更新 ${op.sale_id}：${bits.join('，') || '无变化'}`;
    }
    case 'delete':
      return `删除 ${op.sale_id}`;
    case 'move_store':
      return `门店迁移 ${op.store_id} → ${op.zone_id ? zoneName(op.zone_id) : '未分配'}`;
    default:
      return `未知操作 #${index}`;
  }
}

function renderPending() {
  $('#pending-count').textContent = String(state.pending.length);
  $('#pending-submit').disabled = state.pending.length === 0;
  const list = $('#pending-list');
  if (!state.pending.length) {
    list.replaceChildren(el('p', { class: 'empty-state' }, '没有待提交的变更。修改源表或载入场景后，操作会出现在这里。'));
    return;
  }
  list.replaceChildren(...state.pending.map((op, index) =>
    el('div', { class: 'pending-item' },
      el('span', { class: 'pi-index' }, String(index + 1).padStart(2, '0')),
      el('span', { class: 'pi-desc' }, describeOp(op, index),
        el('span', {}, op.type)),
      el('button', {
        class: 'btn-mini', type: 'button',
        onclick: () => { state.pending.splice(index, 1); renderPending(); },
      }, '移除'))));
}

$('#pending-clear').addEventListener('click', () => { state.pending = []; renderPending(); });
$('#pending-submit').addEventListener('click', async () => {
  if (!state.pending.length) return;
  const payload = {
    label: $('#pending-label').value.trim() || '页面编辑批次',
    strict: $('#pending-strict').checked,
    ops: state.pending,
  };
  try {
    const result = await api('/api/batches', { method: 'POST', body: JSON.stringify(payload) });
    state.pending = [];
    showSubmitResult(result);
    await loadState();
    toast(result.message || '批次已提交', result.status === 'replayed' ? '' : 'good');
    showTab('batches');
  } catch (error) {
    toast(error.message, 'bad');
  }
});

// ----------------------------------------------------------------- batches

async function loadScenarios() {
  const data = await api('/api/scenarios');
  state.scenarios = data.scenarios || [];
  renderScenarios();
}

function renderScenarios() {
  const grid = $('#scenario-grid');
  grid.replaceChildren(...state.scenarios.map((scenario) =>
    el('div', { class: 'scenario-card' },
      el('h3', {}, scenario.title),
      el('p', {}, scenario.summary),
      scenario.expect ? el('p', { class: 'sc-why' }, '预期：' + scenario.expect) : null,
      el('p', { class: 'sc-why' }, scenario.why),
      el('div', { class: 'sc-actions' },
        el('button', {
          class: 'btn small primary', type: 'button',
          onclick: () => loadScenarioIntoEditor(scenario),
        }, '载入编辑器'),
        scenario.retry_payload ? el('button', {
          class: 'btn small ghost', type: 'button',
          onclick: () => loadScenarioIntoEditor(scenario, true),
        }, '载入重试版本') : null,
      ))));
}

function loadScenarioIntoEditor(scenario, retry = false) {
  const payload = retry ? scenario.retry_payload : scenario.payload;
  $('#editor-ops').value = JSON.stringify(payload.ops, null, 2);
  $('#editor-label').value = payload.label || scenario.title;
  $('#editor-strict').checked = Boolean(payload.strict);
  const failInput = document.querySelector('#batch-form input[name="fail_after"]');
  failInput.value = payload.fail_after ?? '';
  $('#editor-hint').textContent = retry ? '已载入重试版本（不含故障注入）' : '已载入场景';
  showTab('batches');
  $('#editor-ops').focus();
}

$('#editor-format').addEventListener('click', () => {
  try {
    const ops = JSON.parse($('#editor-ops').value);
    $('#editor-ops').value = JSON.stringify(ops, null, 2);
    $('#editor-hint').textContent = 'JSON 合法';
  } catch (error) {
    $('#editor-hint').textContent = 'JSON 不合法：' + error.message;
  }
});

$('#batch-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  let ops;
  try {
    ops = JSON.parse($('#editor-ops').value);
  } catch (error) {
    $('#editor-hint').textContent = 'JSON 不合法：' + error.message;
    return;
  }
  if (!Array.isArray(ops)) {
    $('#editor-hint').textContent = '操作必须是数组';
    return;
  }
  const failRaw = document.querySelector('#batch-form input[name="fail_after"]').value.trim();
  const payload = {
    label: $('#editor-label').value.trim() || '手工批次',
    strict: $('#editor-strict').checked,
    ops,
  };
  if (failRaw !== '') payload.fail_after = Number(failRaw);
  try {
    const result = await api('/api/batches', { method: 'POST', body: JSON.stringify(payload) });
    $('#editor-hint').textContent = '';
    showSubmitResult(result);
    await loadState();
    toast(result.message || '批次已提交', result.status === 'rolled_back' ? 'bad'
      : result.status === 'replayed' ? '' : 'good');
  } catch (error) {
    toast(error.message, 'bad');
  }
});

function showSubmitResult(result) {
  const holder = $('#submit-result');
  holder.replaceChildren();
  const statusLabel = {
    committed: '已提交',
    partial: '部分提交（有操作被拒收）',
    mismatch: '已提交但增量与全量不一致',
    replayed: '幂等重放（未再次累计）',
    rolled_back: '已回滚（故障注入）',
    rejected: '整批拒绝（strict）',
  }[result.status] || result.status;

  const card = el('div', { class: 'result-summary' },
    el('div', { class: 'rs-title' },
      el('span', { class: 'tag ' + (result.status === 'committed' ? 'good'
        : result.status === 'rolled_back' || result.status === 'rejected' ? 'bad' : 'warn') },
        statusLabel),
      result.batch ? el('span', { class: 'mono' }, `${result.batch.batch_id} · 快照 v${result.batch.snapshot_version}`) : null),
    el('div', { class: 'rs-body' }, result.message || ''),
  );
  holder.append(card);

  if (result.detail?.note) {
    holder.append(el('div', { class: 'banner warn' },
      el('span', {},
        `${result.detail.note}（故障前已写入 ${result.detail.applied_ops_before_failure} 条操作的明细）`)));
  }

  if (result.rejected?.length) {
    holder.append(el('h4', {}, `被拒收的操作（${result.rejected.length}）`));
    holder.append(el('div', { class: 'rejected-list' },
      ...result.rejected.map((item) => el('div', { class: 'rejected-item' },
        el('div', { class: 'ri-head' },
          el('span', { class: 'ri-code' }, item.code),
          el('span', {}, `#${item.op_index} ${item.op_type} · ${item.sale_id || ''}`)),
        el('div', { class: 'ri-msg' }, item.message),
        el('div', { class: 'ri-raw' }, JSON.stringify(item.raw))))));
  }

  if (result.warnings?.length) {
    const items = result.warnings.flatMap((w) => w.warnings.map((text) => `#${w.op_index} ${text}`));
    if (items.length) {
      holder.append(el('h4', {}, '提示'));
      holder.append(el('div', { class: 'banner' }, el('span', {}, items.join('；'))));
    }
  }

  if (result.group_deltas?.length) {
    const deltas = result.group_deltas;
    const table = el('table', { class: 'delta-table' },
      el('thead', {}, el('tr', {},
        el('th', {}, '分组'), el('th', { class: 'num' }, '撤回订单'), el('th', { class: 'num' }, '加入订单'),
        el('th', { class: 'num' }, '撤回金额'), el('th', { class: 'num' }, '加入金额'),
        el('th', { class: 'num' }, '净订单'), el('th', { class: 'num' }, '净金额'))),
      el('tbody', {}, deltas.map((d) => el('tr', {},
        el('td', {}, zoneName(d.zone_key)),
        el('td', { class: 'num retract' }, num(d.retract_orders)),
        el('td', { class: 'num add' }, num(d.add_orders)),
        el('td', { class: 'num retract' }, money(d.retract_total_cents)),
        el('td', { class: 'num add' }, money(d.add_total_cents)),
        el('td', { class: 'num net' }, num(d.net_orders)),
        el('td', { class: 'num net' }, money(d.net_total_cents))))));
    holder.append(el('h4', {}, '本次批次的逐区域增量'), el('div', { class: 'table-wrap' }, table));
  }

  if (result.snapshot?.summary) {
    holder.append(el('h4', {}, `提交后的汇总（快照 v${result.snapshot.version}）`));
    holder.append(el('div', { class: 'table-wrap' }, renderCompactSummary(result.snapshot.summary)));
  }
  holder.append(el('p', { class: 'one-shot' },
    '批次的增量明细、逐区域净变化、结果汇总、同快照源表副本与全量对照都在同一个事务中提交。'));
}

function renderCompactSummary(rows) {
  if (!rows.length) {
    return el('p', { class: 'empty-state' }, '该快照没有任何分组。');
  }
  return el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '分组'), el('th', { class: 'num' }, '订单数'), el('th', { class: 'num' }, '非空笔数'),
      el('th', { class: 'num' }, '空/零'), el('th', { class: 'num' }, '金额合计'),
      el('th', { class: 'num' }, '客单价'))),
    el('tbody', {}, rows.map((r) => el('tr', {},
      el('td', {}, r.zone_name),
      el('td', { class: 'num' }, num(r.order_count)),
      el('td', { class: 'num' }, num(r.amount_count)),
      el('td', { class: 'num' }, `${r.null_count} / ${r.zero_count}`),
      el('td', { class: 'num' }, money(r.total_cents)),
      el('td', { class: r.unit_price_cents === null ? 'num missing' : 'num' },
        fmtMeasure('unit_price_cents', r.unit_price_cents))))));
}

function renderBatchHistory() {
  const batches = state.data.batches || [];
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '批次'), el('th', {}, '名称'), el('th', { class: 'num' }, '操作'),
      el('th', { class: 'num' }, '接受'), el('th', { class: 'num' }, '拒收'),
      el('th', {}, '状态'), el('th', {}, '时间'), el('th', {}, ''))),
  );
  const body = el('tbody');
  if (!batches.length) {
    body.append(el('tr', {}, el('td', { colspan: 8, class: 'muted' }, '还没有批次。')));
  }
  for (const batch of batches) {
    body.append(el('tr', {},
      el('td', { class: 'mono' }, `${batch.batch_id} → v${batch.snapshot_version}`),
      el('td', {}, batch.label),
      el('td', { class: 'num' }, num(batch.op_count)),
      el('td', { class: 'num' }, num(batch.accepted_count)),
      el('td', { class: 'num' }, num(batch.rejected_count)),
      el('td', {}, el('span', { class: 'tag ' + (batch.status === 'committed' ? 'good'
        : batch.rejected_count ? 'warn' : 'bad') },
        batch.status === 'committed' ? '已提交' : batch.status === 'partial' ? '部分提交' : batch.status)),
      el('td', { class: 'one-shot' }, batch.applied_at.replace('T', ' ').replace('+00:00', ' UTC')),
      el('td', {},
        el('div', { class: 'row-edit' },
          el('button', {
            class: 'btn-mini', type: 'button',
            onclick: () => showBatchDetail(batch.batch_id),
          }, '明细'),
          el('button', {
            class: 'btn-mini', type: 'button',
            onclick: () => replayBatch(batch.batch_id),
          }, '重放')))));
  }
  table.append(body);
  $('#batch-history').replaceChildren(table);
}

async function showBatchDetail(batchId) {
  const detail = await api(`/api/batches/${encodeURIComponent(batchId)}`);
  state.batchDetail = detail;
  const holder = $('#batch-detail');
  holder.hidden = false;
  holder.replaceChildren(
    el('h4', {}, `${detail.batch.batch_id} · ${detail.batch.label}`),
    el('p', { class: 'one-shot' },
      `基准快照 v${detail.batch.base_version ?? '—'} → 结果快照 v${detail.batch.snapshot_version}` +
      ` · 操作 ${detail.batch.op_count} · 接受 ${detail.batch.accepted_count} · 拒收 ${detail.batch.rejected_count}` +
      ` · 状态 ${detail.batch.status}`),
    el('h4', {}, '操作明细'),
    el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, '#'), el('th', {}, '类型'), el('th', {}, '主键'),
        el('th', {}, '结果'), el('th', {}, '说明'), el('th', {}, ''))),
      el('tbody', {}, detail.ops.map((op) => el('tr', {},
        el('td', { class: 'num' }, String(op.idx)),
        el('td', {}, op.op_type),
        el('td', { class: 'mono' }, op.payload.sale_id || op.payload.store_id || '—'),
        el('td', {}, op.result === 'applied'
          ? el('span', { class: 'tag good' }, '已应用')
          : el('span', { class: 'tag bad' }, op.error_code)),
        el('td', { class: 'one-shot' },
          op.result === 'applied'
            ? (op.warnings?.length ? op.warnings.join('；') : '—')
            : op.error_message),
        el('td', {}, op.result === 'applied'
          ? el('button', {
              class: 'btn-mini', type: 'button',
              onclick: () => { showTab('impact'); selectImpact(detail.batch.batch_id, op.idx); },
            }, '查看影响')
          : null))))),
    ),
    el('h4', {}, '逐区域增量'),
    el('div', { class: 'table-wrap' }, el('table', { class: 'delta-table' },
      el('thead', {}, el('tr', {},
        el('th', {}, '分组'), el('th', { class: 'num' }, '撤回订单'), el('th', { class: 'num' }, '加入订单'),
        el('th', { class: 'num' }, '撤回金额'), el('th', { class: 'num' }, '加入金额'),
        el('th', { class: 'num' }, '净订单'), el('th', { class: 'num' }, '净金额'))),
      el('tbody', {}, detail.group_deltas.map((d) => el('tr', {},
        el('td', {}, zoneName(d.zone_key)),
        el('td', { class: 'num retract' }, num(d.retract_orders)),
        el('td', { class: 'num add' }, num(d.add_orders)),
        el('td', { class: 'num retract' }, money(d.retract_total_cents)),
        el('td', { class: 'num add' }, money(d.add_total_cents)),
        el('td', { class: 'num net' }, num(d.net_orders)),
        el('td', { class: 'num net' }, money(d.net_total_cents))))))),
  );
}

async function replayBatch(batchId) {
  const detail = await api(`/api/batches/${encodeURIComponent(batchId)}`);
  const payload = {
    label: detail.batch.label,
    strict: detail.batch.strict,
    ops: detail.payload?.ops || detail.ops.map((op) => op.payload),
  };
  try {
    const result = await api('/api/batches', { method: 'POST', body: JSON.stringify(payload) });
    showSubmitResult(result);
    await loadState();
    toast(result.status === 'replayed'
      ? '相同批次内容被识别为幂等重放：没有再次累计'
      : result.message, result.status === 'replayed' ? '' : 'good');
  } catch (error) {
    toast(error.message, 'bad');
  }
}

// ------------------------------------------------------------------ impact

function renderImpactSelectors() {
  const batchSelect = $('#impact-batch');
  const current = batchSelect.value;
  const batches = state.data.batches || [];
  batchSelect.replaceChildren(el('option', { value: '' }, '选择批次…'),
    ...batches.map((batch) => el('option', { value: batch.batch_id },
      `${batch.batch_id} · ${batch.label}`)));
  batchSelect.value = batches.some((b) => b.batch_id === current) ? current : (batches[0]?.batch_id || '');
  refreshImpactOps();
}

async function refreshImpactOps() {
  const batchId = $('#impact-batch').value;
  const opSelect = $('#impact-op');
  opSelect.replaceChildren();
  if (!batchId) return;
  const detail = await api(`/api/batches/${encodeURIComponent(batchId)}`);
  const options = detail.ops
    .filter((op) => op.result === 'applied')
    .map((op) => el('option', { value: String(op.idx) },
      `#${op.idx} ${op.op_type} · ${op.payload.sale_id || op.payload.store_id || ''}`));
  opSelect.replaceChildren(...(options.length ? options : [el('option', { value: '' }, '没有已应用的操作')]));
}

$('#impact-batch').addEventListener('change', refreshImpactOps);
$('#impact-load').addEventListener('click', () => {
  const batchId = $('#impact-batch').value;
  const opIndex = $('#impact-op').value;
  if (!batchId || opIndex === '') return;
  loadImpact(batchId, Number(opIndex));
});

async function selectImpact(batchId, opIndex) {
  $('#impact-batch').value = batchId;
  await refreshImpactOps();
  $('#impact-op').value = String(opIndex);
  loadImpact(batchId, opIndex);
}

async function loadImpact(batchId, opIndex) {
  const holder = $('#impact-body');
  if (batchId === undefined) {
    const selectedBatch = $('#impact-batch').value;
    const selectedOp = $('#impact-op').value;
    if (!selectedBatch || selectedOp === '') {
      holder.replaceChildren(el('p', { class: 'empty-state' }, '先选择一个批次和一条已应用的操作。'));
      return;
    }
    batchId = selectedBatch;
    opIndex = Number(selectedOp);
  }
  const impact = await api(`/api/batches/${encodeURIComponent(batchId)}/ops/${opIndex}/impact`);
  state.impact = impact;
  holder.replaceChildren(impactCard(impact));
}

function impactCard(impact) {
  const op = impact.op;
  const card = el('div', { class: 'impact-card' },
    el('div', { class: 'impact-head' },
      el('span', { class: 'tag' }, `#${op.idx} ${op.op_type}`),
      el('strong', {}, op.payload.sale_id || op.payload.store_id || ''),
      el('span', { class: 'one-shot' },
        `批次 ${impact.batch_id} · 快照 v${impact.base_version ?? '—'} → v${impact.snapshot_version}`)),
  );

  if (!impact.zones.length) {
    card.append(el('p', { class: 'empty-state' },
      '这条操作没有产生任何分组增减（例如门店迁移但没有有效销售）。'));
    return card;
  }

  const zonesWrap = el('div', { class: 'impact-zones' });
  for (const zone of impact.zones) {
    const beforeIsAbsent = !zone.before_exists;
    const afterIsAbsent = !zone.after_exists;
    zonesWrap.append(el('div', { class: 'impact-zone' },
      el('div', { class: 'impact-zone-title' },
        el('span', { class: 'dot', style: { background: zoneColor(zone.zone_key) } }),
        el('span', { class: 'izo-name' }, zone.zone_name),
        el('span', { class: 'one-shot' },
          `撤回订单 ${zone.entry_retract_orders} / 加入订单 ${zone.entry_add_orders}`),
        zone.entry_retract_total_cents || zone.entry_add_total_cents
          ? el('span', { class: 'one-shot' },
              `· 撤回金额 ${money(-zone.entry_retract_total_cents)} / 加入金额 ${money(zone.entry_add_total_cents)}`)
          : null),
      el('div', { class: 'before-after' },
        el('div', { class: 'ba-box' + (beforeIsAbsent ? ' absent' : '') },
          el('h5', {}, '变更前（上一快照）'),
          beforeIsAbsent
            ? el('div', { class: 'absent-text' }, '无分组：该区域此前没有任何销售')
            : metricList(zone.before)),
        el('div', { class: 'ba-arrow' }, '→'),
        el('div', { class: 'ba-box' + (afterIsAbsent ? ' absent' : '') },
          el('h5', {}, '变更后（本批次快照）'),
          afterIsAbsent
            ? el('div', { class: 'absent-text' },
                zone.before?.order_count ? '无分组：该区域的销售已被全部撤回' : '无分组')
            : metricList(zone.after)),
      ),
    ));
  }
  card.append(zonesWrap);

  card.append(el('h4', { style: { marginTop: '16px' } }, '该操作写入的增量明细'));
  card.append(el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '阶段'), el('th', {}, '销售'), el('th', {}, '分组'),
      el('th', { class: 'num' }, '订单'), el('th', { class: 'num' }, '非空'),
      el('th', { class: 'num' }, '空'), el('th', { class: 'num' }, '零'),
      el('th', { class: 'num' }, '金额'), el('th', {}, '原因'))),
    el('tbody', {}, impact.entries.map((entry) => el('tr', {},
      el('td', {}, el('span', { class: 'tag ' + (entry.phase === 'retract' ? 'bad' : 'good') },
        entry.phase === 'retract' ? '撤回' : '加入')),
      el('td', { class: 'mono' }, entry.sale_id),
      el('td', {}, zoneName(entry.zone_key)),
      el('td', { class: 'num' }, num(entry.order_delta)),
      el('td', { class: 'num' }, num(entry.amount_count_delta)),
      el('td', { class: 'num' }, num(entry.null_count_delta)),
      el('td', { class: 'num' }, num(entry.zero_count_delta)),
      el('td', { class: 'num' }, money(entry.total_delta_cents)),
      el('td', { class: 'one-shot' }, entry.reason)))))));
  return card;
}

function metricList(row) {
  return el('div', { class: 'mini-metrics' },
    ...MEASURES.map(([key, label]) => el('div', { class: 'mm' },
      el('span', {}, label),
      el('b', { class: row[key] === null ? 'missing' : '' }, fmtMeasure(key, row[key])))));
}

// ------------------------------------------------------------------- audit

function renderAuditSelectors() {
  const select = $('#audit-version');
  const versions = (state.data.batches || []).map((b) => b.snapshot_version).sort((a, b) => b - a);
  select.replaceChildren(...versions.map((v) => el('option', { value: String(v) }, `快照 v${v}`)));
  if (!versions.length) return;
  // Follow the latest snapshot after every state refresh (a new batch must not
  // leave the comparison pinned to an older version than the header shows).
  // The change handler below still switches to whatever version the user picks.
  select.value = String(versions[0]);
  runAudit(versions[0]);
}

$('#audit-recompute').addEventListener('click', () => {
  const version = Number($('#audit-version').value);
  if (version) runAudit(version);
});
$('#audit-version').addEventListener('change', () => {
  const version = Number($('#audit-version').value);
  if (version) runAudit(version);
});

async function runAudit(version) {
  const holder = $('#audit-result');
  holder.replaceChildren(el('p', { class: 'empty-state' }, '正在按同一快照重算…'));
  try {
    const result = await api(`/api/snapshots/${version}/recompute`);
    state.audit = result;
    renderAudit(result);
  } catch (error) {
    holder.replaceChildren(el('div', { class: 'banner bad' }, el('span', {}, error.message)));
  }
}

function renderAudit(result) {
  const holder = $('#audit-result');
  const mismatches = result.comparison.filter((c) => !c.is_match);
  holder.replaceChildren(
    el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {},
          el('h3', {}, `快照 v${result.version} 的逐项对照`),
          el('p', { class: 'card-note' },
            '左侧是持久化的增量汇总，右侧是用该快照自己的源表副本完整重算的结果。逐区域、逐指标比对。'))
        ),
      el('div', { class: 'audit-summary' },
        el('div', { class: 'stat' },
          el('div', { class: 'stat-label' }, '对照项'),
          el('div', { class: 'stat-value' }, String(result.comparison.length)),
          el('div', { class: 'stat-sub' }, `${new Set(result.comparison.map((c) => c.zone_key)).size} 个分组 × 6 项指标`)),
        el('div', { class: 'stat' },
          el('div', { class: 'stat-label' }, '不一致项'),
          el('div', { class: 'stat-value', style: { color: mismatches.length ? 'var(--critical)' : 'var(--good-text)' } },
            String(mismatches.length)),
          el('div', { class: 'stat-sub' }, mismatches.length ? '需要检查增量链路' : '增量结果与全量一致')),
        el('div', { class: 'stat' },
          el('div', { class: 'stat-label' }, '存储的全量副本'),
          el('div', { class: 'stat-value' }, result.stored_full_matches.every((c) => c.is_match) ? '一致' : '有差异'),
          el('div', { class: 'stat-sub' }, '提交时落盘的全量结果与本次重算')),
      ),
      el('div', { class: 'table-wrap' }, el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, '分组'), el('th', {}, '指标'), el('th', { class: 'num' }, '增量汇总'),
          el('th', { class: 'num' }, '全量重算'), el('th', {}, '结果'))),
        el('tbody', {}, result.comparison.map((item) => el('tr', {},
          el('td', {}, item.zone_name),
          el('td', {}, MEASURES.find(([key]) => key === item.measure)?.[1] || item.measure),
          el('td', { class: 'num' }, fmtMeasure(item.measure, item.incremental_value)),
          el('td', { class: 'num' }, fmtMeasure(item.measure, item.full_value)),
          el('td', {}, el('span', { class: 'match-cell ' + (item.is_match ? 'ok' : 'bad') },
            item.is_match ? '✓ 一致' : '✗ 不一致'))))))),
    ),
  );
  if (!result.comparison.length) {
    holder.prepend(el('div', { class: 'banner' },
      el('span', {}, '该快照没有任何分组：全量重算与增量汇总都为空，两者一致。')));
  }
}

function renderAttempts() {
  const attempts = state.data.attempts || [];
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '时间'), el('th', {}, '批次名称'), el('th', {}, '状态'),
      el('th', {}, '错误码'), el('th', { class: 'num' }, '已写入操作'), el('th', {}, '说明'))),
  );
  const body = el('tbody');
  if (!attempts.length) {
    body.append(el('tr', {}, el('td', { colspan: 6, class: 'muted' }, '还没有提交尝试。')));
  }
  for (const attempt of attempts) {
    const badge = attempt.status === 'committed' ? 'good'
      : attempt.status === 'partial' ? 'warn'
      : ['rolled_back', 'rejected_strict'].includes(attempt.status) ? 'bad' : '';
    body.append(el('tr', {},
      el('td', { class: 'one-shot' }, attempt.created_at.replace('T', ' ').replace('+00:00', ' UTC')),
      el('td', {}, attempt.label || '—'),
      el('td', {}, el('span', { class: 'tag ' + badge },
        { committed: '已提交', partial: '部分提交', rolled_back: '已回滚', rejected_strict: '整批拒绝' }[attempt.status] || attempt.status)),
      el('td', { class: 'mono' }, attempt.error_code || '—'),
      el('td', { class: 'num' }, num(attempt.applied_ops)),
      el('td', { class: 'one-shot' }, attempt.error || (attempt.batch_id ? `批次 ${attempt.batch_id}` : '—'))));
  }
  table.append(body);
  $('#attempts-table').replaceChildren(table);
}

$('#reset-data').addEventListener('click', async () => {
  if (!confirm('这会删除本机数据库并重新写入虚构样例。确定继续？')) return;
  try {
    await api('/api/reset', { method: 'POST', body: '{}' });
    state.pending = [];
    state.impact = null;
    await loadState();
    toast('已重置为初始样例', 'good');
    showTab('summary');
  } catch (error) {
    toast(error.message, 'bad');
  }
});

// -------------------------------------------------------------------- boot

(async function boot() {
  applyTheme(localStorage.getItem('theme') || 'light');
  await loadScenarios();
  await loadState();
  $('#summary-compare-toggle').checked = state.summaryCompare;
  if (state.data.batches?.length) {
    // Make the first run legible: show the seeded batch in the editor.
    const seed = state.data.batches[state.data.batches.length - 1];
    if (seed) {
      const detail = await api(`/api/batches/${encodeURIComponent(seed.batch_id)}`);
      $('#editor-ops').value = JSON.stringify(detail.ops.map((op) => op.payload), null, 2);
      $('#editor-label').value = seed.label;
    }
  }
})();
