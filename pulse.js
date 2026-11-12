// Market Pulse: LVPC BuildLV monthly figures (public data, read through the public `market_pulse` view).
// Compares the latest year to date with the same months of the year before.
window.loadPulse = async function (sb) {
  const { data, error } = await sb.from('market_pulse').select('*').order('month', { ascending: true });
  if (error || !data || !data.length) return null;
  const rows = data.map(r => ({ ...r, y: +r.month.slice(0, 4), m: +r.month.slice(5, 7) }));
  const year = Math.max(...rows.map(r => r.y));
  const cur = rows.filter(r => r.y === year);
  const months = cur.map(r => r.m);
  const prev = rows.filter(r => r.y === year - 1 && months.includes(r.m));
  const sum = (list, k) => list.reduce((t, r) => t + (Number(r[k]) || 0), 0);
  const pct = k => (prev.length === cur.length && sum(prev, k) > 0) ? Math.round((sum(cur, k) / sum(prev, k) - 1) * 100) : null;
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const latest = cur[cur.length - 1];
  return {
    year, latest, months,
    range: MON[Math.min(...months) - 1] + (months.length > 1 ? '–' + MON[Math.max(...months) - 1] : '') + ' ' + year,
    sqft: sum(cur, 'nonres_sqft_total'), sqftPct: pct('nonres_sqft_total'),
    industrial: sum(cur, 'industrial_sqft'),
    units: sum(cur, 'units_total'), unitsPct: pct('units_total'),
    plans: sum(cur, 'plans'), plansPct: pct('plans'),
    series: months.map(m => ({ m: MON[m - 1],
      cur: Number((cur.find(r => r.m === m) || {}).nonres_sqft_total) || 0,
      prev: Number((prev.find(r => r.m === m) || {}).nonres_sqft_total) || 0 })),
    hasPrev: prev.length > 0,
  };
};

window.pulseFmt = {
  sqft(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'K' : String(n); },
  pct(p) { return p == null ? '' : (p >= 0 ? '+' : '−') + Math.abs(p) + '%'; },
  arrow(p) { return p == null ? '' : (p >= 0 ? '▲ ' : '▼ ') + Math.abs(p) + '%'; },
};

// Bar chart: this year (navy) against the same month last year (gray), million sq ft per month.
window.pulseChart = function (P, w, h, opts) {
  opts = opts || {};
  const pad = opts.axis ? 28 : 0, base = h - pad, n = P.series.length;
  const max = Math.max(1, ...P.series.map(s => Math.max(s.cur, s.prev)));
  const slot = w / n, bw = opts.axis ? Math.min(22, slot / 3) : Math.min(14, slot * 0.7);
  let out = opts.axis ? `<line x1="0" x2="${w}" y1="${base}" y2="${base}" stroke="#d9dde3"/>` : '';
  P.series.forEach((s, i) => {
    const x = i * slot + (slot - (opts.axis ? bw * 2 + 4 : bw)) / 2;
    const hh = v => Math.max(0.5, (v / max) * (base - 4));
    if (opts.axis && P.hasPrev) out += `<rect x="${x}" y="${base - hh(s.prev)}" width="${bw}" height="${hh(s.prev)}" rx="2" fill="#cfd6df"><title>${s.m} ${P.year - 1}: ${pulseFmt.sqft(s.prev)} sq ft</title></rect>`;
    const cx = opts.axis ? x + bw + 4 : x;
    out += `<rect x="${cx}" y="${base - hh(s.cur)}" width="${bw}" height="${hh(s.cur)}" rx="1.5" fill="#1F3A5F"><title>${s.m} ${P.year}: ${pulseFmt.sqft(s.cur)} sq ft</title></rect>`;
    if (opts.axis) out += `<text class="ax" x="${i * slot + slot / 2}" y="${h - 8}" text-anchor="middle">${s.m}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" class="${opts.cls || ''}" role="img" aria-label="Monthly non-residential square feet proposed">${out}</svg>`;
};
