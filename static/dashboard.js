'use strict';

const $ = id => document.getElementById(id);
const controls = $('controls');
const form = $('upload');
const chart = $('smile');
const useBrowser = document.body.dataset.calculator === 'browser';
let worker;
let sequence = 0;
const pending = new Map();

function browserRequest(action, payload) {
  if (!worker) {
    worker = new Worker('./static/browser-worker.mjs', { type: 'module' });
    worker.onmessage = ({ data: { id, result, error } }) => {
      const callback = pending.get(id);
      if (!callback) return;
      pending.delete(id);
      if (error) callback.reject(new Error(error));
      else callback.resolve(result);
    };
    worker.onerror = () => {
      for (const callback of pending.values()) {
        callback.reject(new Error('The browser pricing engine could not start. Reload the page and try again.'));
      }
      pending.clear();
      worker.terminate();
      worker = undefined;
    };
  }
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, action, payload });
  });
}
const state = { dataset: null, catalog: null, result: null, hour: 0, full: false, hover: null, chartPoints: [], busy: false };
const unit = (x, digits = 2) => x == null ? '—' : Number(x).toFixed(digits);
const points = (x, digits = 2) => x == null ? '—' : x.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const pct = (x, digits = 2) => x == null ? '—' : (x * 100).toFixed(digits) + '%';
const signed = (x, digits = 3) => x == null ? '—' : (x > 0 ? '+' : '') + x.toFixed(digits);
const gap = x => x == null ? '—' : signed(x * 100) + ' pp';
const hhmm = x => x ? x.slice(11, 16) : '—';
const day = x => x ? x.slice(0, 10) : '—';
const dateTime = x => x ? day(x) + ' ' + hhmm(x) + ' CT' : '—';
const escape = x => String(x ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const put = (id, value) => { $(id).textContent = value; };
const list = (id, entries) => {
  $(id).innerHTML = entries.map(([label, value]) => '<div><dt>' + escape(label) + '</dt><dd>' + escape(value) + '</dd></div>').join('');
};
const tableRow = cells => '<tr>' + cells.map(v => '<td>' + escape(v) + '</td>').join('') + '</tr>';

async function request(url, body, formData = false) {
  if (useBrowser) {
    return browserRequest(url.split('/').at(-1), formData ? Object.fromEntries(body) : body);
  }
  const response = await fetch(url, body ? {
    method: 'POST',
    ...(formData ? { body } : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  } : undefined);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The dashboard request failed.');
  return data;
}

function message(text, isError = false) {
  put('message', text);
  $('message').classList.toggle('error', isError);
}

async function action(task, status) {
  if (state.busy) return;
  state.busy = true;
  const fields = [...form.elements, ...controls.elements, $('reset')];
  fields.forEach(el => { el.disabled = true; });
  message(status);
  try {
    await task();
    message('');
  } catch (error) {
    message(error.message, true);
  } finally {
    fields.forEach(el => { el.disabled = false; });
    state.busy = false;
  }
}

// A disabled form control is omitted from FormData, so read settings by name.
function modelOptions() {
  const selected = { dataset: state.dataset };
  for (const el of controls.elements) if (el.name && el.value !== '') selected[el.name] = el.value;
  return selected;
}

function selectOptions(id, entries, value) {
  const select = $(id);
  select.replaceChildren(...entries.map(([key, name]) => new Option(name, key)));
  if (value != null && entries.some(([key]) => String(key) === String(value))) select.value = value;
}

function describeCatalog(info, keepSPX = false) {
  const prevES = $('es').value;
  const prevSPX = $('spx').value;
  state.catalog = info;
  selectOptions('es', info.es.map(e => [e.id, e.code + ' · ' + day(e.expiry) + ' · ' +
    (e.dte == null ? 'set expiry' : e.dte + ' DTE')]), prevES);
  selectOptions('spx', info.spx.map(e => [e.id, e.code + ' · ' + e.dte + ' DTE · ' + day(e.expiry) +
    ' ' + hhmm(e.expiry) + (e.gap_hours === 0 ? ' · exact' : '')]), keepSPX ? prevSPX : info.default_spx);
  const es = info.es.find(e => String(e.id) === $('es').value);
  const spx = info.spx.find(e => String(e.id) === $('spx').value);
  put('selection-note', es.future + ' · SPX settlement ' + dateTime(spx.expiry) +
    ' · SPX raw expiry marker ' + spx.raw_expiry.replace('T', ' ') + ' · source DTE ' + (spx.vendor_dte.join(', ') || '—'));
}

async function calculate(keepHour = true) {
  if (!state.dataset) throw new Error('Load the example or select both source files.');
  const previous = keepHour ? state.result?.hours[state.hour]?.time : null;
  $('results').hidden = true;
  state.result = await request('/api/compare', modelOptions());
  const index = state.result.hours.findIndex(h => h.time === previous);
  const first = state.result.hours.findIndex(h => h.summary.matched);
  state.hour = index >= 0 ? index : first >= 0 ? first : 0;
  selectOptions('hour', state.result.hours.map((h, i) => [i, day(h.time) + ' ' + hhmm(h.time)]), state.hour);
  state.hover = null;
  renderResults();
  $('results').hidden = false;
  drawChart();
}

async function loadDataset(payload) {
  state.dataset = payload.dataset;
  state.result = null;
  $('results').hidden = true;
  selectOptions('es', []);
  selectOptions('spx', []);
  selectOptions('hour', []);
  controls.reset();
  put('spx-name', payload.catalog.files.spx);
  put('es-name', payload.catalog.files.es);
  put('future-name', payload.catalog.files.futures || 'Optional CSV');
  describeCatalog(payload.catalog);
  if (!payload.catalog.effective_es.expiry || !payload.catalog.effective_es.style) {
    $('settings').open = true;
    throw new Error('Set the ES date, time and exercise style, then run the comparison.');
  }
  await calculate(false);
}

function renderResults() {
  const r = state.result;
  const h0 = r.hours[0];
  const hlast = r.hours.at(-1);
  const snapshotCount = r.hours.length;
  put('session', (day(h0.time) === day(hlast.time) ? day(h0.time) : day(h0.time) + ' to ' + day(hlast.time)) +
    ' · ' + snapshotCount + ' ES snapshot' + (snapshotCount === 1 ? '' : 's'));
  $('contracts').innerHTML = '<strong>' + escape(r.spx.code) + '</strong> / <strong>' + escape(r.es.code) +
    '</strong> · ES expiry ' + escape(dateTime(r.es.expiry));
  put('pricing', r.es.style === 'american' ? 'American · CRR futures tree' : 'European · Black–76');
  const notices = [];
  if (r.using_actual) notices.push('ES pricing uses the uploaded futures price for each exact contract and snapshot match.');
  else notices.push('ES pricing uses the workbook’s put–call parity forward as a proxy for ' + r.es.future + '. An actual futures price can be uploaded for an independent check.');
  if (r.spx.naive) notices.push('SPX timestamps are interpreted in ' + r.zone + '.');
  if (r.expiry_gap_hours !== 0) notices.push('SPX settlement differs by ' + signed(r.expiry_gap_hours, 2) +
    ' hours. The mapping carries IV across that maturity difference.');
  if (!r.overlap) notices.push('No exact hourly matches with the selected timezone and series.');
  $('basis').innerHTML = notices.map(n => '<p>' + escape(n) + '</p>').join('');

  const s = r.summary;
  const metrics = [
    ['Matched strike-hours', s.matched + ' / ' + s.total, r.overlap + ' / ' + snapshotCount + ' overlapping snapshots'],
    ['Median ΔIV', gap(s.median_gap), 'ES midpoint IV − mapped SPX IV'],
    ['Mean absolute ΔIV', s.mae == null ? '—' : (s.mae * 100).toFixed(3) + ' pp',
      s.price_mae == null ? 'No matched premiums' : unit(s.price_mae, 3) + ' pt mean absolute price error'],
    ['Model inside ES spread', pct(s.in_spread, 1), 'Share of matched quotes']
  ];
  $('metrics').innerHTML = metrics.map(([name, value, note]) => '<div class="metric"><div class="metric-name">' +
    escape(name) + '</div><div class="metric-value">' + escape(value) + '</div><small>' + escape(note) + '</small></div>').join('');
  $('wing-body').innerHTML = r.buckets.map(b => tableRow([b.name, b.matched + ' / ' + b.total,
    gap(b.median_gap), pct(b.in_spread, 1)])).join('');
  $('timezone').innerHTML = r.timing.map(x => tableRow([x.zone, x.overlap, unit(x.rmse) +
    (x.changes ? ' (' + x.changes + (x.changes === 1 ? ' pair)' : ' pairs)') : '')])).join('');
  list('series-diagnostics', [
    ['SPX settlement', dateTime(r.spx.expiry)], ['ES settlement', dateTime(r.es.expiry)],
    ['SPX settlement − ES settlement', signed(r.expiry_gap_hours, 2) + ' h'],
    ['SPX raw expiry marker', r.spx.raw_expiry], ['SPX expiry basis', r.spx.source],
    ['ES expiry basis', r.es.source], ['Flat rate / SPX file rate', pct(r.rate) + ' / ' +
      (r.spx.rates.length ? r.spx.rates.map(x => pct(x)).join(', ') : '—')],
    ['SPX / ES source rows', points(state.catalog.inputs.spx_rows, 0) + ' / ' + points(state.catalog.inputs.es_rows, 0)],
    ['Invalid keys / duplicate rows', (state.catalog.inputs.invalid_spx + state.catalog.inputs.invalid_es) +
      ' / ' + state.catalog.inputs.duplicates]
  ]);
  list('exclusions', Object.entries(r.status_counts).length ? Object.entries(r.status_counts) : [['Unmatched ES quotes', 0]]);
  renderHour();
}

const strikeKey = q => q.strike + '|' + q.side;

function renderHour() {
  const r = state.result;
  const h = r.hours[state.hour];
  const s = h.summary;
  const display = dateTime(h.time);
  $('hour').value = state.hour;
  $('hours-body').innerHTML = r.hours.map((x, i) => '<tr' + (i === state.hour ? ' class="active"' : '') +
    '><td><button type="button" class="table-link" data-hour="' + i + '">' +
    escape(x.time.slice(5, 10) + ' ' + hhmm(x.time)) + '</button></td><td>' + x.summary.matched + ' / ' + x.summary.total +
    '</td><td>' + gap(x.summary.median_gap) + '</td><td>' + pct(x.summary.in_spread, 1) + '</td></tr>').join('');
  put('smile-caption', display + ' · ' + s.matched + ' of ' + s.total + ' ES quotes mapped');
  const prior = $('strike').value;
  const union = [...new Map(r.hours.flatMap(x => x.rows).map(q => [strikeKey(q), q])).values()];
  union.sort((a, b) => a.strike - b.strike || a.side.localeCompare(b.side));
  selectOptions('strike', [['', 'All strikes'], ...union.map(q => [strikeKey(q), points(q.strike, 0) + ' ' + q.side])], prior);
  renderStrikes();
  const sf = h.surface;
  const sourceDte = r.spx.vendor_dte.length ? r.spx.vendor_dte.join(', ') : '—';
  list('hour-diagnostics', [
    ['SPX / ES exact DTE (days)', unit(h.spx_dte, 3) + ' / ' + unit(h.es_dte, 3)],
    ['SPX source DTE (file field)', sourceDte],
    ['SPX implied forward', points(sf?.forward)],
    [r.using_actual ? 'ES futures price' : 'ES parity forward', points(h.es_forward)],
    ['ES − SPX forward (points)', sf && h.es_forward != null ? signed(h.es_forward - sf.forward, 2) : '—'],
    ['SPX near-forward parity pairs', sf?.pairs ?? '—'],
    ['SPX parity forward MAD / range (pts)', unit(sf?.parity_mad, 3) + ' / ' + unit(sf?.parity_span, 3)],
    ['SPX calibrated moneyness range', sf ? pct(sf.k_min) + ' to ' + pct(sf.k_max) : '—'],
    ['SPX ATM IV / ES nearest strike IV', pct(h.atm_spx) + ' / ' + pct(h.atm_es?.es_iv)],
    ['ES nearest strike to ATM', h.atm_es ? points(h.atm_es.strike, 0) + ' ' + h.atm_es.side : '—'],
    ['ES capture lag / source quote times', unit(h.capture_lag, 3) + ' s / ' + h.exchange_timestamp_rows + ' of ' + s.total],
    ['ES time anchor', (h.captured_slot ? 'Capture time' : 'Scheduled slot') + (h.es_naive ? ' · assumed CT' : '')]
  ]);
  state.hover = null;
  put('hover-value', 'Hover over the chart to inspect an ES quote.');
  drawChart();
}

function renderStrikes() {
  const r = state.result;
  if (!r) return;
  const h = r.hours[state.hour];
  const strike = $('strike').value;
  const all = $('coverage-filter').value === 'all';
  const rows = h.rows.filter(q => (all || strike || q.gap != null) && (!strike || strikeKey(q) === strike));
  put('strike-caption', dateTime(h.time) + ' · ' + rows.length + ' of ' + h.rows.length + ' quotes shown');
  $('strike-body').innerHTML = rows.length ? rows.map(q => tableRow([
    points(q.strike, 2), q.side, pct(q.k), points(q.bid) + ' / ' + points(q.ask), pct(q.es_iv, 3),
    pct(q.mapped_iv, 3), gap(q.gap), points(q.model_price, 3), signed(q.price_gap, 3),
    q.bracket_low == null ? '—' : points(q.bracket_low, 0) + ' / ' + points(q.bracket_high, 0),
    q.within == null ? q.status : q.within ? 'Inside spread' : q.model_price < q.bid ? 'Below bid' : 'Above ask'
  ])).join('') : '<tr><td colspan="11">No quotes for this selection. Change the snapshot or coverage filter.</td></tr>';
  $('strike-detail').hidden = !strike;
  if (strike) {
    const q = rows[0];
    const history = r.hours.flatMap(t => t.rows.filter(x => strikeKey(x) === strike).map(x => ({ ...x, time: t.time })));
    const historyStats = history.filter(x => x.within != null);
    const detail = q && q.spx_equivalent_strike != null ? 'Equivalent SPX strike ' + points(q.spx_equivalent_strike, 3) +
      ' · ES quote IV band ' + pct(q.es_bid_iv, 3) + ' to ' + pct(q.es_ask_iv, 3) + ' · ' : '';
    put('strike-detail', detail + historyStats.length + ' / ' + history.length + ' matched snapshots for this strike and side across the loaded history.');
  }
}

function drawChart() {
  if (!state.result || $('results').hidden) return;
  const hour = state.result.hours[state.hour];
  const surface = hour.surface?.nodes || [];
  const rows = hour.rows.filter(q => q.es_iv != null).sort((a, b) => a.k - b.k);
  const dpr = window.devicePixelRatio || 1;
  const w = chart.clientWidth;
  const h = chart.clientHeight;
  if (w < 20 || h < 20) return;
  chart.width = w * dpr;
  chart.height = h * dpr;
  const ctx = chart.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.font = '12px system-ui';
  state.chartPoints = [];
  const bounds = state.full || surface.length < 2 ? [...surface, ...rows] : surface;
  if (bounds.length < 2) {
    ctx.fillStyle = '#abbcd0';
    ctx.textAlign = 'center';
    ctx.fillText('No volatility smile at this snapshot', w / 2, h / 2);
    return;
  }
  const low = Math.min(...bounds.map(q => q.k));
  const high = Math.max(...bounds.map(q => q.k));
  const pad = Math.max((high - low) * .045, .003);
  const xmin = low - pad;
  const xmax = high + pad;
  const visible = rows.filter(q => q.k >= xmin && q.k <= xmax);
  const values = [...surface.map(q => q.iv),
    ...visible.flatMap(q => [q.es_iv, q.es_bid_iv, q.es_ask_iv]).filter(x => x != null)];
  const minVol = Math.min(...values);
  const maxVol = Math.max(...values);
  const ypad = Math.max((maxVol - minVol) * .095, .003);
  const ymin = Math.max(0, minVol - ypad);
  const ymax = maxVol + ypad;
  const left = 67, right = w - 17, top = 17, bottom = h - 48;
  const xf = k => left + (k - xmin) * (right - left) / (xmax - xmin);
  const yf = iv => bottom - (iv - ymin) * (bottom - top) / (ymax - ymin);
  function rule(x1, y1, x2, y2, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }
  ctx.fillStyle = '#abbcd0';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 5; i++) {
    const value = ymin + (ymax - ymin) * i / 5;
    const y = yf(value);
    rule(left, y, right, y, '#304153');
    ctx.fillText(pct(value, 1), left - 8, y + 4);
  }
  ctx.textAlign = 'center';
  const divs = w < 520 ? 4 : 8;
  for (let i = 0; i <= divs; i++) {
    const k = xmin + (xmax - xmin) * i / divs;
    const x = xf(k);
    rule(x, top, x, bottom, '#263748');
    ctx.fillText(pct(k, 1), x, bottom + 18);
  }
  if (xmin < 0 && xmax > 0) rule(xf(0), top, xf(0), bottom, '#587080');
  ctx.fillText('Forward moneyness  log(K/F)', (left + right) / 2, h - 8);
  ctx.save();
  ctx.translate(14, (top + bottom) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText('Annualized IV', 0, 0);
  ctx.restore();
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, top, right - left, bottom - top);
  ctx.clip();
  const band = [];
  for (const q of rows) {
    if (q.es_bid_iv == null || q.es_ask_iv == null) {
      band.push([]);
    } else {
      if (!band.length) band.push([]);
      band.at(-1).push(q);
    }
  }
  ctx.fillStyle = 'rgba(255,204,131,.19)';
  for (const block of band.filter(x => x.length > 1)) {
    const edge = [...block.map(q => [q.k, q.es_ask_iv]),
      ...block.slice().reverse().map(q => [q.k, q.es_bid_iv])];
    ctx.beginPath();
    edge.forEach(([k, iv], i) => i ? ctx.lineTo(xf(k), yf(iv)) : ctx.moveTo(xf(k), yf(iv)));
    ctx.closePath();
    ctx.fill();
  }

  function line(data, ivKey, color, points = false, maxGap = Infinity) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    data.forEach((q, i) => i && q.k - data[i - 1].k <= maxGap ?
      ctx.lineTo(xf(q.k), yf(q[ivKey])) : ctx.moveTo(xf(q.k), yf(q[ivKey])));
    ctx.stroke();
    if (points) {
      ctx.fillStyle = color;
      for (const q of visible) {
        ctx.beginPath();
        ctx.arc(xf(q.k), yf(q[ivKey]), 2.1, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
  line(rows, 'es_iv', '#ffcc83', true);
  line(surface, 'iv', '#75e1ce', false, state.result.max_gap);
  if (state.hover) {
    const { k, es_iv: iv } = state.hover;
    rule(xf(k), top, xf(k), bottom, '#abbcd0');
    ctx.strokeStyle = '#ffcc83';
    ctx.fillStyle = '#17222e';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(xf(k), yf(iv), 5, 0, 2 * Math.PI);
    ctx.fill();
    ctx.stroke();
  }
  ctx.restore();
  state.chartPoints = visible.map(q => ({ x: xf(q.k), q }));
}

function exportCSV() {
  const r = state.result;
  if (!r) return;
  const records = r.hours.flatMap(h => h.rows).map(q => ({
    es_contract: r.es.code, spx_contract: r.spx.code, futures_contract: r.es.future,
    es_settlement_CT: r.es.expiry, spx_settlement_CT: r.spx.expiry, rate_decimal: r.rate,
    es_price_basis: r.using_actual ? 'Uploaded futures' : 'Workbook parity proxy',
    assumed_spx_timezone: r.zone, exercise_style: r.es.style, ...q
  }));
  if (!records.length) return;
  const keys = Object.keys(records[0]);
  const csv = value => {
    if (value == null) return '';
    if (typeof value !== 'string') return String(value);
    const text = /^[=+@\-\t\r\n]/.test(value) ? "'" + value : value;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  const file = [keys.join(','), ...records.map(r => keys.map(k => csv(r[k])).join(','))].join('\r\n');
  const url = URL.createObjectURL(new Blob([file], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'spx-es-' + day(r.hours[0].time) + '.csv';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

form.addEventListener('submit', event => {
  event.preventDefault();
  const files = new FormData(form);
  if (!files.get('spx')?.size || !files.get('es')?.size) {
    message('Select an SPX Parquet and an ES options CSV.', true);
    return;
  }
  action(async () => loadDataset(await request('/api/upload', files, true)),
    useBrowser ? 'Opening the files in your browser. The first load may take a moment…' : 'Loading files and series…');
});
controls.addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    describeCatalog(await request('/api/catalog', modelOptions()), true);
    await calculate();
  }, 'Recomputing the comparison…');
});
$('reset').addEventListener('click', () => action(async () => {
  const example = await request('/api/sample');
  form.reset();
  await loadDataset(example);
}, 'Loading the example…'));
$('es').addEventListener('change', () => action(async () => {
  for (const name of ['es_expiry', 'es_clock', 'es_style', 'spx_expiry', 'spx_clock']) controls.elements[name].value = '';
  describeCatalog(await request('/api/catalog', modelOptions()));
  await calculate();
}, 'Selecting the closest SPX expiry…'));
$('spx').addEventListener('change', () => action(async () => {
  controls.elements.spx_expiry.value = '';
  controls.elements.spx_clock.value = '';
  describeCatalog(await request('/api/catalog', modelOptions()), true);
  await calculate();
}, 'Building the selected SPX surface…'));
$('hour').addEventListener('change', e => {
  state.hour = Number(e.target.value);
  renderHour();
});
$('hours-body').addEventListener('click', event => {
  const index = event.target.dataset.hour;
  if (index == null) return;
  state.hour = Number(index);
  renderHour();
  document.querySelector('[data-hour="' + index + '"]').focus();
});
$('coverage-filter').addEventListener('change', renderStrikes);
$('strike').addEventListener('change', renderStrikes);
$('download').addEventListener('click', exportCSV);
for (const id of ['focus', 'full']) $(id).addEventListener('click', () => {
  state.full = id === 'full';
  state.hover = null;
  $('focus').setAttribute('aria-pressed', !state.full);
  $('full').setAttribute('aria-pressed', state.full);
  drawChart();
});
chart.addEventListener('pointermove', event => {
  if (!state.chartPoints.length) return;
  const x = event.clientX - chart.getBoundingClientRect().left;
  const nearest = state.chartPoints.reduce((a, b) => Math.abs(a.x - x) < Math.abs(b.x - x) ? a : b);
  if (state.hover === nearest.q) return;
  state.hover = nearest.q;
  const q = nearest.q;
  put('hover-value', points(q.strike, 0) + ' ' + q.side + ' · ES ' + pct(q.es_iv, 3) +
    ' · SPX mapped ' + pct(q.mapped_iv, 3) + ' · ' + gap(q.gap));
  drawChart();
});
chart.addEventListener('pointerleave', () => {
  state.hover = null;
  put('hover-value', 'Hover over the chart to inspect an ES quote.');
  drawChart();
});
new ResizeObserver(drawChart).observe(chart);
if (!useBrowser) {
  action(async () => loadDataset(await request('/api/sample')), 'Opening the example files…');
}
