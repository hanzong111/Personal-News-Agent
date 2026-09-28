/* Pipeline console front end. Vanilla JS, no build step.
   State: runs (by id) with their events; overview from ticks; live events via SSE. */
(() => {
  const $ = (s) => document.querySelector(s);
  const TZ = 'Asia/Kuala_Lumpur';
  const KINDS = ['scan', 'news', 'digest', 'curate', 'weekly', 'ops', 'sweep', 'lss6'];
  const state = {
    runs: new Map(),        // id -> {summary, events:[]}
    order: [],              // run ids newest first
    selected: null,
    overview: null,
    log: [],                // last N events for the log panel
    logFollow: true,
  };
  const LOG_MAX = 400;

  // ---------------------------------------------------------------- utils
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString('en-GB', { timeZone: TZ, hour12: false });
  const fmtDate = (ts) => new Date(ts * 1000).toLocaleString('en-GB', { timeZone: TZ, hour12: false, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  const fmtDur = (s) => s == null ? '–' : s < 60 ? `${s.toFixed(1)}s` : s < 3600 ? `${Math.floor(s / 60)}m${Math.round(s % 60)}s` : `${(s / 3600).toFixed(1)}h`;
  const fmtTok = (n) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const tag = (v) => `<span class="tag t-${esc(v)}">${esc(v)}</span>`;
  const kindClass = (k) => `k-${KINDS.includes(k) ? k : 'other'}`;
  const api = (p) => fetch(p).then((r) => r.json());

  // ---------------------------------------------------------------- run bookkeeping
  function summarise(id, evs) {
    // mirrors data.summarise_run for runs that arrive live (server summary used when available)
    const llm = evs.filter((e) => e.node === 'llm' && e.msg === 'call done');
    const term = [...evs].reverse().find((e) => e.msg === 'message' || e.msg === 'silent' || (e.node === 'ops' && e.msg === 'check'));
    const errors = evs.filter((e) => e.level === 'ERROR').length;
    const nodes = []; evs.forEach((e) => { if (!nodes.includes(e.node)) nodes.push(e.node); });
    const gate = evs.find((e) => e.msg === 'gate');
    return {
      id, kind: id.split('-')[0], start: evs[0].ts, end: evs[evs.length - 1].ts, dur: evs[evs.length - 1].ts - evs[0].ts,
      events: evs.length, nodes, llm_calls: llm.length,
      tok_in: llm.reduce((a, e) => a + (e.tok_in || 0), 0), tok_out: llm.reduce((a, e) => a + (e.tok_out || 0), 0),
      usd: +llm.reduce((a, e) => a + (e.usd || 0), 0).toFixed(4),
      result: term ? (term.msg === 'check' ? (term.new ? 'findings' : 'ok') : term.msg) : (errors ? 'error' : 'running'),
      lines: term?.lines, errors, finished: !!term, gate: gate || null,
      mode: evs.find((e) => e.msg === 'run start')?.mode, hermes: state.runs.get(id)?.summary?.hermes || null,
    };
  }

  function upsertRun(summary, events) {
    const cur = state.runs.get(summary.id);
    if (cur) { cur.summary = { ...cur.summary, ...summary }; if (events) cur.events = events; }
    else {
      state.runs.set(summary.id, { summary, events: events || [] });
      state.order.push(summary.id);
      state.order.sort((a, b) => state.runs.get(b).summary.start - state.runs.get(a).summary.start);
    }
  }

  function latestByKind() {
    const out = {};
    for (const id of state.order) { const k = state.runs.get(id).summary.kind; if (!out[k]) out[k] = id; }
    return out;
  }

  // ---------------------------------------------------------------- executing-node rule
  // Openers: span starts ("collect …"), "start", "call start". Each stays open on its node until the
  // matching closer arrives: "<name> done|failed|<name>" for spans, "done|plan|failed" for start,
  // "call done|failed" for llm, final "chunk done k/k" for the classifier. Other events don't close.
  function spanKey(e) {
    if (e.msg.endsWith('…')) return { open: e.msg.slice(0, -1).trim() };
    if (e.msg === 'start' || e.msg === 'call start') return { open: e.msg };
    if (e.msg === 'call done' || e.msg === 'call failed') return { close: 'call start' };
    if (e.msg === 'done' || e.msg === 'plan' || e.msg === 'failed' || e.msg === 'nothing to edit' || e.msg === 'no candidates') return { close: 'start' };
    if (e.msg === 'chunk done' && typeof e.chunk === 'string' && /^(\d+)\/\1$/.test(e.chunk)) return { close: 'start' };
    const m = e.msg.match(/^(.*?)(?: (done|failed))?$/);
    return { close: m ? m[1] : e.msg };     // "collect done" / "prices" / "yahoo history" close their opener
  }
  function executingNodes(run) {
    if (run.summary.finished) return {};
    const open = {};                        // node -> Map(spanName -> ts)
    run.events.forEach((e) => {
      const k = spanKey(e);
      if (e.level === 'ERROR') { delete open[e.node]; return; }
      if (k.open) { (open[e.node] ||= new Map()).set(k.open, e.ts); }
      else if (k.close && open[e.node]) { open[e.node].delete(k.close); if (!open[e.node].size) delete open[e.node]; }
    });
    const out = {};
    Object.entries(open).forEach(([n, m]) => { out[n] = Math.min(...m.values()); });
    return out;
  }
  const isLive = (run) => !run.summary.finished && (Date.now() / 1000 - run.summary.end) < 900;

  // ---------------------------------------------------------------- render: header / jobs
  function renderOverview() {
    const o = state.overview; if (!o) return;
    const gw = $('#led-gw'); gw.className = 'led ' + (o.gateway === 'active' ? 'on' : o.gateway === 'unknown' ? 'warn' : 'off');
    $('#evbytes').textContent = `events.jsonl ${(o.events_file_bytes / 1024).toFixed(0)} KB`;
    const rows = o.jobs.map((j) => {
      const next = j.next_run ? Date.parse(j.next_run) / 1000 : null;
      const st = j.paused ? 'paused' : (j.last_status || 'none');
      return `<tr><td class="${kindClass(j.kind)}">${esc(j.name)}</td><td>${esc(j.schedule || '')}</td>
        <td class="countdown" data-next="${next || ''}">${next ? countdown(next) : '–'}</td>
        <td>${tag(st)}</td><td class="dim">${esc((j.model || (j.no_agent ? 'no-agent' : 'default')).replace('claude-', ''))}</td></tr>`;
    }).join('');
    $('#jobs').innerHTML = `<tr><th>job</th><th>cron</th><th>next</th><th>last</th><th>model</th></tr>${rows}`;
    $('#queues').innerHTML = `<span>digest queue <b>${o.queues.digest}</b></span><span>judge queue <b>${o.queues.judge}</b></span>`;
    const f = o.watchdog?.findings || [];
    const box = $('#findings');
    if (f.length) { box.classList.remove('hidden'); box.innerHTML = `<div class="head" style="font-size:9px;margin-bottom:6px">WATCHDOG · last 24 h</div>` + f.map((x) => `<div>${esc(fmtTime(x.ts))} ${esc(x.text)}</div>`).join(''); }
    else box.classList.add('hidden');
  }

  function countdown(nextTs) {
    const d = Math.max(0, nextTs - Date.now() / 1000);
    if (d === 0) return 'due';
    const h = Math.floor(d / 3600), m = Math.floor((d % 3600) / 60), s = Math.floor(d % 60);
    return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }

  function tickClock() {
    $('#clock').textContent = new Date().toLocaleTimeString('en-GB', { timeZone: TZ, hour12: false }) + ' MYT';
    document.querySelectorAll('.countdown[data-next]').forEach((el) => { if (el.dataset.next) el.textContent = countdown(+el.dataset.next); });
  }

  // ---------------------------------------------------------------- render: nodes
  function nodeStat(node, evs) {
    const mine = evs.filter((e) => e.node === node);
    const last = mine[mine.length - 1];
    if (node === 'fetch' || node === 'news.fetch') {
      const items = mine.reduce((a, e) => a + (e.items || 0), 0);
      return `${mine.length} calls · ${items} items`;
    }
    if (node === 'llm') {
      const done = mine.filter((e) => e.msg === 'call done');
      return `${done.length} call${done.length === 1 ? '' : 's'} · ${fmtTok(done.reduce((a, e) => a + (e.tok_in || 0), 0))}→${fmtTok(done.reduce((a, e) => a + (e.tok_out || 0), 0))}`;
    }
    if (node === 'scan') {
      const g = mine.find((e) => e.msg === 'gate');
      if (g) return `new ${g.new} · alert ${g.alerts} · q ${g.queued_for_digest}`;
    }
    if (node === 'news.resolve' && last) return `${last.new ?? 0} new · ${last.cache_hits ?? 0} cached`;
    if (node === 'renderer' && last) return `${last.lines ?? ''} lines`;
    if (node.startsWith('agent.') && last) return esc(last.msg) + (last.dur ? ` ${fmtDur(last.dur)}` : '');
    return last ? esc(last.msg) : '';
  }

  function renderNodes(flashNode) {
    const latest = latestByKind();
    const html = KINDS.filter((k) => latest[k]).map((k) => {
      const r = state.runs.get(latest[k]); const s = r.summary; const evs = r.events;
      const running = isLive(r);
      const exec = running ? executingNodes(r) : {};
      const anyOpen = Object.keys(exec).length > 0;
      const lastNode = s.nodes[s.nodes.length - 1];
      const now = Date.now() / 1000;
      const boxes = s.nodes.map((n) => {
        const err = evs.some((e) => e.node === n && e.level === 'ERROR');
        const executing = running && (exec[n] != null || (!anyOpen && n === lastNode));
        const cls = err ? 'error' : executing ? 'active' : 'done';
        const flash = flashNode && flashNode.run === s.id && flashNode.node === n ? ' flash' : '';
        const stat = executing ? `<span class="exec">▶ executing ${fmtDur(now - (exec[n] ?? s.end))}</span>` : nodeStat(n, evs);
        return `<div class="node ${cls}${flash}" data-node="${esc(n)}"><i class="lamp"></i><div class="n">${esc(n)}</div><div class="s">${stat}</div></div>`;
      });
      // synthetic hermes stage
      const h = s.hermes;
      let hcls = 'hermes pending', hstat = 'no cron match';
      if (h) {
        const open = !h.delivery && h.status !== 'failed';
        hcls = 'hermes ' + (h.status === 'failed' || h.delivery === 'failed' ? 'error' : open ? 'active' : 'done');
        hstat = open ? `<span class="exec">▶ ${esc(h.status)} ${fmtDur(now - (h.started || s.end))}</span>` : `${h.delivery || h.status}${h.dur != null ? ' · ' + fmtDur(h.dur) : ''}`;
      } else if (s.mode && s.mode !== 'cron') { hstat = `manual (${esc(s.mode)})`; }
      boxes.push(`<div class="node ${hcls}"><i class="lamp"></i><div class="n">hermes ▸ ${esc(h?.job || 'cron')}</div><div class="s">${hstat}</div></div>`);
      return `<div class="flow">
        <div class="flow-head"><span class="kind ${kindClass(k)}">${k.toUpperCase()}</span>
          <span class="meta"><b>${esc(s.id)}</b> · ${fmtDate(s.start)} · ${fmtDur(s.dur)} · ${tag(s.result)}${s.llm_calls ? ` · ${s.llm_calls} llm · $${s.usd.toFixed(4)}` : ''}</span></div>
        <div class="chain">${boxes.join('<span class="arrow">▶</span>')}</div></div>`;
    }).join('');
    $('#nodes').innerHTML = html || '<div class="dim">no runs in the log yet</div>';
  }
  setInterval(() => { if ([...state.runs.values()].some(isLive)) renderNodes(null); }, 1000);

  // ---------------------------------------------------------------- render: runs / timeline
  function renderRuns() {
    const rows = state.order.slice(0, 40).map((id) => {
      const s = state.runs.get(id).summary;
      const h = s.hermes;
      return `<tr data-id="${esc(id)}" class="${id === state.selected ? 'sel' : ''}">
        <td>${fmtDate(s.start)}</td><td class="${kindClass(s.kind)}">${esc(s.kind)}</td><td class="num">${fmtDur(s.dur)}</td>
        <td>${tag(s.result)}</td><td class="num">${s.llm_calls || ''}</td><td class="num">${s.llm_calls ? fmtTok(s.tok_in) + '→' + fmtTok(s.tok_out) : ''}</td>
        <td class="num">${s.usd ? '$' + s.usd.toFixed(3) : ''}</td><td>${h ? tag(h.delivery || h.status) : (s.mode && s.mode !== 'cron' ? `<span class="dim">${esc(s.mode)}</span>` : '')}</td></tr>`;
    }).join('');
    $('#runs').innerHTML = `<tr><th>start</th><th>kind</th><th>dur</th><th>result</th><th>llm</th><th>tokens</th><th>usd</th><th>hermes</th></tr>${rows}`;
  }

  async function selectRun(id) {
    state.selected = id;
    const r = state.runs.get(id);
    if (r && r.events.length === 0) {
      const d = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (d.events) upsertRun(d.run, d.events);
    }
    renderRuns(); renderTimeline();
  }

  function renderTimeline() {
    const r = state.selected && state.runs.get(state.selected);
    if (!r || !r.events.length) { $('#timeline').innerHTML = ''; $('#timeline-title').textContent = 'select a run'; return; }
    const evs = r.events; const t0 = evs[0].ts; const span = Math.max(0.5, evs[evs.length - 1].ts - t0);
    $('#timeline-title').textContent = `${r.summary.id} · ${fmtDur(span)} · ${evs.length} events`;
    const rows = evs.filter((e) => e.level !== 'DEBUG' || e.dur != null).map((e) => {
      const err = e.level === 'ERROR' ? ' err' : '';
      const cls = e.node === 'llm' ? ' llm' : e.node.startsWith('agent.') ? ' agent' : '';
      let mark;
      if (e.dur != null) {
        const left = Math.max(0, (e.ts - e.dur - t0) / span * 100), width = Math.max(0.5, e.dur / span * 100);
        mark = `<div class="tl-bar${cls}${err}" style="left:${left}%;width:${Math.min(width, 100 - left)}%" title="${fmtDur(e.dur)}"></div>`;
      } else {
        mark = `<div class="tl-tick${err}" style="left:${(e.ts - t0) / span * 100}%"></div>`;
      }
      const detail = e.role ? `${e.role}` : e.holding ? `${e.holding}` : e.query ? `${e.query}` : e.source ? `${e.source}` : '';
      return `<div class="tl-row"><div class="lbl" title="${esc(e.msg)}"><b>${esc(e.node)}</b> ${esc(e.msg)}${detail ? ' · ' + esc(detail) : ''}</div><div class="tl-track">${mark}</div></div>`;
    }).join('');
    $('#timeline').innerHTML = `<div class="tl-scale"><span>+0s</span><span>+${fmtDur(span / 2)}</span><span>+${fmtDur(span)}</span></div>${rows}`;
  }

  // ---------------------------------------------------------------- render: log
  function fmtEvent(e) {
    const extra = Object.entries(e).filter(([k]) => !['ts', 'level', 'node', 'run', 'msg'].includes(k))
      .map(([k, v]) => `${k}=${typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(2)) : JSON.stringify(v)}`).join(' ');
    return `<span class="ts">${fmtTime(e.ts)}</span> <span class="l-${e.level}">[${e.level.padEnd(5)}]</span> <span class="nd">[${esc(e.node)}]</span> ${esc(e.msg)} <span class="kv">${esc(extra)}</span>`;
  }

  const LEVELS = { DEBUG: 10, INFO: 20, WARN: 30, ERROR: 40 };
  function logVisible(e) {
    const node = $('#log-node').value, lvl = LEVELS[$('#log-level').value] || 10;
    return (!node || e.node.startsWith(node)) && (LEVELS[e.level] || 20) >= lvl;
  }

  function renderLog() {
    const el = $('#log');
    el.innerHTML = state.log.filter(logVisible).map((e) => `<div class="l-${e.level}">${fmtEvent(e)}</div>`).join('');
    if ($('#log-follow').checked) el.scrollTop = el.scrollHeight;
    // node filter options
    const sel = $('#log-node'); const have = new Set([...sel.options].map((o) => o.value));
    new Set(state.log.map((e) => e.node.split('.')[0])).forEach((n) => { if (!have.has(n)) { const o = document.createElement('option'); o.value = n; o.textContent = n; sel.appendChild(o); } });
  }

  function appendLog(e) {
    state.log.push(e); if (state.log.length > LOG_MAX) state.log.shift();
    if (!logVisible(e)) return;
    const el = $('#log'); const div = document.createElement('div'); div.className = `l-${e.level} new`; div.innerHTML = fmtEvent(e);
    el.appendChild(div); while (el.children.length > LOG_MAX) el.removeChild(el.firstChild);
    if ($('#log-follow').checked) el.scrollTop = el.scrollHeight;
  }

  // ---------------------------------------------------------------- render: health / spend
  function renderHealth(h) {
    const src = Object.entries(h.sources).filter(([k]) => k !== 'http').map(([k, v]) => {
      const bad = v.errors > 0;
      return `<div class="tile"><div class="k">${esc(k)}</div><div class="v ${bad ? 'warn' : ''}">${v.calls}</div><div class="d">${v.items} items${bad ? ` · ${v.errors} err` : ''}</div></div>`;
    });
    const runs = Object.entries(h.runs).map(([k, v]) => `<div class="tile"><div class="k ${kindClass(k)}">${esc(k)} runs</div><div class="v">${Object.values(v).reduce((a, b) => a + b, 0)}</div><div class="d">${Object.entries(v).map(([r, n]) => `${r} ${n}`).join(' · ')}</div></div>`);
    const del = Object.entries(h.deliveries).map(([k, v]) => `<div class="tile"><div class="k">hermes ${esc(k)}</div><div class="v ${k === 'failed' ? 'bad' : ''}">${v}</div></div>`);
    const llm = `<div class="tile"><div class="k">llm 24h</div><div class="v ${h.llm.errors ? 'bad' : ''}">$${h.llm.usd.toFixed(3)}</div><div class="d">${h.llm.calls} calls · ${fmtTok(h.llm.tok_in)}→${fmtTok(h.llm.tok_out)}${h.llm.errors ? ` · ${h.llm.errors} err` : ''}</div></div>`;
    const over = h.overruns.length ? `<div class="tile"><div class="k">overruns</div><div class="v warn">${h.overruns.length}</div><div class="d">${esc(h.overruns.map((o) => `${o.job} ${fmtDur(o.dur)}`).join(', '))}</div></div>` : '';
    $('#health').innerHTML = [llm, ...runs, ...del, over, ...src].join('');
  }

  function renderSpend(s) {
    const roles = Object.entries(s.by_role).sort((a, b) => b[1].usd - a[1].usd);
    const total = roles.reduce((a, [, v]) => a + v.usd, 0) || 1;
    const rows = roles.map(([r, v]) => `<div class="bar-row"><span>${esc(r)} <span class="dim" style="font-size:13px">${esc((v.model || '').replace('claude-', ''))}</span></span><div class="b" style="width:${(v.usd / total * 100).toFixed(1)}%"></div><span class="r">$${v.usd.toFixed(3)}</span></div>`);
    const days = Object.entries(s.by_day).map(([d, v]) => { const usd = Object.values(v).reduce((a, x) => a + x.usd, 0); return `<div class="bar-row"><span>${esc(d.slice(5))}</span><div class="b" style="width:${Math.min(100, usd / total * 100).toFixed(1)}%;background:var(--cyan)"></div><span class="r">$${usd.toFixed(3)}</span></div>`; });
    $('#spend').innerHTML = `<div class="bars">${rows.join('')}</div><div class="bars" style="margin-top:10px">${days.join('')}</div><div class="dim" style="margin-top:6px">total 7 d: $${total.toFixed(3)} (your agents only; hermes cron sessions not included)</div>`;
  }

  // ---------------------------------------------------------------- render: cost ledger
  const fmtCount = (n) => Number(n || 0).toLocaleString('en-US');
  const usd = (n) => n == null ? '–' : `$${Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const rm = (n) => n == null ? '–' : `RM ${Number(n).toLocaleString('en-MY', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  function renderCosts(c) {
    const breakdown = $('#cost-breakdown');
    if (!c || !c.exists) {
      $('#cost-hero').innerHTML = `<div class="cost-empty">${esc(c?.error || 'No ledger yet — cost-ledger writes data/state/costs.db every 10 minutes.')}</div>`;
      $('#cost-tiles').innerHTML = '';
      $('#cost-jobs').innerHTML = '';
      breakdown.classList.add('hidden');
      return;
    }
    const rate = c.usdmyr;
    const span = c.span_days < 1 ? `${Math.max(1, Math.round(c.span_days * 24))} h` : `${c.span_days.toFixed(1)} d`;
    $('#cost-title').textContent = `since ${fmtIso(c.start)} · ${span} · every model call · API-equivalent`;
    $('#cost-hero').innerHTML = [
      `<div class="cost-score tokens"><div class="k"><i>01</i>TOTAL TOKENS</div><div class="v">${fmtCount(c.tokens)}</div><div class="d">${fmtCount(c.tok_in)} input (${fmtCount(c.cached)} cached) · ${fmtCount(c.tok_out)} output</div></div>`,
      `<div class="cost-score usd"><div class="k"><i>02</i>USD VALUE</div><div class="v">${usd(c.usd)}</div><div class="d">list price equivalent · ${fmtCount(c.calls)} calls</div></div>`,
      `<div class="cost-score myr"><div class="k"><i>03</i>ROUGHLY IN RM</div><div class="v">${c.myr == null ? '–' : `≈ ${rm(c.myr)}`}</div><div class="d">${rate ? `${rate.toFixed(3)} MYR / USD · rate ${esc(c.usdmyr_day || '')}` : 'exchange rate not fetched yet'}</div></div>`,
    ].join('');
    const ch = c.chat || {};
    $('#cost-tiles').innerHTML = [
      `<div class="tile"><div class="k">today</div><div class="v">${usd(c.today?.usd)}</div><div class="d">${fmtCount(c.today?.tokens)} tokens · ${fmtCount(c.today?.calls)} calls</div></div>`,
      `<div class="tile"><div class="k">per day</div><div class="v">${usd(c.per_day_usd)}</div><div class="d">${rate ? rm(c.per_day_usd * rate) : ''} avg since start</div></div>`,
      `<div class="tile"><div class="k">month pace</div><div class="v ${c.month_usd > 60 ? 'warn' : ''}">${usd(c.month_usd)}</div><div class="d">${rm(c.month_myr)} / 30 d at this rate</div></div>`,
      `<div class="tile"><div class="k">chat / msg</div><div class="v">${ch.per_message_usd == null ? '–' : '$' + ch.per_message_usd.toFixed(3)}</div><div class="d">${ch.messages || 0} msgs · chat total ${usd(ch.usd)}</div></div>`,
    ].join('');
    const top = c.by_category.slice(0, 10), max = Math.max(...top.map((v) => v.usd), 1e-9);
    const rows = top.map((v) => `<div class="bar-row"><span title="${esc(v.category)}">${esc(v.category.replace(/^(pipeline|cron|chat):/, ''))} <span class="cost-kind">${esc(v.category.split(':')[0])}</span></span><div class="bar-track"><i style="width:${(v.usd / max * 100).toFixed(1)}%"></i></div><span class="r">${usd(v.usd)}</span></div>`).join('');
    $('#cost-jobs').innerHTML = rows + (c.unpriced?.length ? `<div class="cost-note">no price for ${esc(c.unpriced.join(', '))} · counted as $0</div>` : '');
    breakdown.classList.toggle('hidden', !rows && !c.unpriced?.length);
  }
  const refreshCosts = () => api('/api/costs').then(renderCosts).catch(() => {});

  // ---------------------------------------------------------------- render: memory
  const STAGES = ['seen', 'digest', 'judge', 'alert', 'alerted', 'digested', 'dropped'];
  const fmtBytes = (n) => n >= 1048576 ? `${(n / 1048576).toFixed(2)} MB` : `${(n / 1024).toFixed(0)} KB`;
  const fmtIso = (iso) => iso ? fmtDate(Date.parse(iso) / 1000) : '–';
  const ageDays = (iso) => iso ? (Date.now() - Date.parse(iso)) / 86400000 : null;
  const fmtAge = (iso) => { const d = ageDays(iso); return d == null ? '–' : d < 1 / 24 ? `${Math.round(d * 1440)}m ago` : d < 1 ? `${Math.round(d * 24)}h ago` : `${d.toFixed(1)}d ago`; };
  function renderMemory(m) {
    if (!m || !m.exists) { $('#memory-tiles').innerHTML = `<div class="dim">${esc(m?.error || 'news.db not found — run pipeline.memory migrate')}</div>`; return; }
    const names = m.names || {};
    const name = (c) => names[c] || c;
    const bs = m.by_stage;
    const lp = m.last_prune;
    const pruneCounts = lp ? Object.entries(lp).filter(([k, v]) => typeof v === 'number').map(([k, v]) => `${k} ${v}`).join(' · ') : '';
    const overdueDetail = Object.entries(m.overdue || {}).filter(([, v]) => v > 0).map(([k, v]) => `${k} ${v}`).join(' · ');
    const tiles = [
      `<div class="tile"><div class="k">news.db</div><div class="v">${fmtBytes(m.bytes)}</div><div class="d">${m.items} items · ${m.keys} keys · ${m.urls} urls</div></div>`,
      `<div class="tile"><div class="k">pending</div><div class="v ${m.pending > 300 ? 'warn' : ''}">${m.pending}</div><div class="d">alert ${bs.alert} · digest ${bs.digest} · judge ${bs.judge}</div></div>`,
      `<div class="tile"><div class="k">delivered</div><div class="v">${bs.alerted + bs.digested}</div><div class="d">alerted ${bs.alerted} · digested ${bs.digested} · dropped ${bs.dropped}</div></div>`,
      `<div class="tile"><div class="k">stories</div><div class="v">${m.stories}</div><div class="d">${m.open_stories.length} open · ${m.notes} notes</div></div>`,
      `<div class="tile"><div class="k">overdue</div><div class="v ${m.overdue_total ? 'warn' : ''}">${m.overdue_total}</div><div class="d">${m.overdue_total ? esc(overdueDetail) : `past policy ${m.retention.raw}/${m.retention.urls}/${m.retention.delivered} d`}</div></div>`,
      `<div class="tile"><div class="k">last prune</div><div class="v ${lp ? (ageDays(lp.at) > 1.2 ? 'warn' : '') : 'warn'}" style="font-size:18px">${lp ? esc(fmtAge(lp.at)) : 'never'}</div><div class="d">${lp ? esc(pruneCounts) + (lp.vacuum ? ' · VACUUM' : '') : 'bursa-curate 19:00 Mon–Fri'}</div></div>`,
      `<div class="tile"><div class="k">span</div><div class="v" style="font-size:18px">${m.oldest ? `${(ageDays(m.oldest)).toFixed(1)} d` : '–'}</div><div class="d">oldest ${esc(fmtIso(m.oldest))} · newest ${esc(fmtAge(m.newest))}</div></div>`,
    ];
    $('#memory-tiles').innerHTML = tiles.join('');
    // stage strip
    const total = m.items || 1;
    const segs = STAGES.filter((s) => bs[s] > 0).map((s) => `<i class="st-${s}" style="width:${(bs[s] / total * 100).toFixed(2)}%" title="${s} ${bs[s]}"></i>`);
    const legend = STAGES.map((s) => `<span><i class="st-${s}"></i>${s} <b>${bs[s]}</b></span>`);
    $('#memory-stages').innerHTML = `<div class="stage-bar">${segs.join('')}</div><div class="stage-legend">${legend.join('')}</div>`;
    // stories
    $('#memory-stories-title').textContent = `open · last ${m.days} d`;
    const rows = m.open_stories.map((st) => `<tr><td class="dim">${esc(fmtAge(st.last_seen))}</td><td>${esc(st.codes.map(name).join(' '))}</td><td class="n">${st.n_items}</td><td>${tag(st.type || 'other')}</td><td class="t" title="${esc(st.title)}">${esc(st.title)}</td></tr>`);
    $('#memory-stories').innerHTML = rows.length ? `<tr><th>seen</th><th>holding</th><th>n</th><th>type</th><th>title</th></tr>${rows.join('')}` : '<tr><td class="dim">no open stories</td></tr>';
    // notes
    const notes = m.notes_list.map((n) => `<div class="note ${ageDays(n.updated) > m.retention.notes ? 'stale' : ''}" title="${esc(n.text)}"><div class="nh"><b>${esc(name(n.key))}</b><span>${esc(n.kind)}</span><span>${esc(fmtAge(n.updated))}</span><span>${n.words} w</span></div><div class="nt">${esc(n.preview)}</div></div>`);
    $('#memory-notes').innerHTML = notes.length ? notes.join('') : '<div class="dim">no notes yet — the curator writes one per holding it touches</div>';
  }
  let memoryTimer = null;
  const refreshMemory = () => api('/api/memory?days=7').then(renderMemory).catch(() => {});

  // ---------------------------------------------------------------- live
  let flashTimer = null;
  function onEvent(e) {
    const id = e.run || '-';
    if (id !== '-') {
      const r = state.runs.get(id);
      const evs = r ? [...r.events, e] : [e];
      upsertRun(summarise(id, evs), evs);
      renderNodes({ run: id, node: e.node });
      clearTimeout(flashTimer); flashTimer = setTimeout(() => renderNodes(null), 700);
      if (state.order.indexOf(id) < 40) renderRuns();
      if (state.selected === id) renderTimeline();
      if (e.msg === 'message' || e.msg === 'silent' || e.msg === 'run start') { setTimeout(refreshRuns, 2500); setTimeout(refreshRuns, 15000); }   // pick up the hermes ledger row (open, then closed)
      if (e.msg === 'message' || e.msg === 'silent') { clearTimeout(memoryTimer); memoryTimer = setTimeout(refreshMemory, 1500); }   // stages advance at the end of a run
    }
    appendLog(e);
  }

  function connect() {
    const es = new EventSource('/api/stream');
    const led = $('#led-sse');
    es.onopen = () => { led.className = 'led on'; };
    es.onerror = () => { led.className = 'led off'; };
    es.addEventListener('tick', (m) => { state.overview = JSON.parse(m.data); renderOverview(); led.className = 'led on'; });
    es.addEventListener('event', (m) => onEvent(JSON.parse(m.data)));
  }

  async function refreshRuns() {
    const rs = await api('/api/runs?n=40');
    rs.forEach((s) => upsertRun(s));
    renderRuns(); renderNodes(null);
  }

  async function init() {
    const [rs, evs, h, s, m, c] = await Promise.all([
      api('/api/runs?n=40'), api('/api/events?n=300'), api('/api/health?hours=24'),
      api('/api/spend?days=7'), api('/api/memory?days=7'),
      api('/api/costs').catch((e) => ({ exists: false, error: e.message })),
    ]);
    rs.forEach((r) => upsertRun(r));
    state.log = evs; renderLog();

    // Paint the useful overview immediately; detailed event payloads hydrate in the background.
    renderRuns(); renderNodes(null); renderHealth(h); renderSpend(s); renderMemory(m); renderCosts(c);
    connect();
    setInterval(tickClock, 1000); tickClock();

    const selected = state.order[0];
    if (selected) selectRun(selected);
    Promise.allSettled(Object.values(latestByKind()).filter((id) => id !== selected).map(async (id) => {
      const d = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (d.events) upsertRun(d.run, d.events);
    })).then(() => renderNodes(null));
    setInterval(async () => { renderHealth(await api('/api/health?hours=24')); renderSpend(await api('/api/spend?days=7')); refreshMemory(); refreshCosts(); }, 60000);
  }

  $('#runs').addEventListener('click', (ev) => { const tr = ev.target.closest('tr[data-id]'); if (tr) selectRun(tr.dataset.id); });
  $('#log-node').addEventListener('change', renderLog);
  $('#log-level').addEventListener('change', renderLog);
  $('#log').addEventListener('scroll', () => { const el = $('#log'); if (el.scrollHeight - el.scrollTop - el.clientHeight > 40) $('#log-follow').checked = false; });

  init().catch((e) => { document.body.insertAdjacentHTML('afterbegin', `<div class="findings">init failed: ${esc(e.message)}</div>`); });
})();
