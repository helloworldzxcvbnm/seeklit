/* ==========================================================================
   溯文 SeekLit —— 前端工作台
   原生 JS，无构建步骤、无外部依赖。所有数据来自同源 FastAPI。
   设计原则：界面上出现的每一个分数、每一段 SQL，都是 seekdb 真实返回的，
   不做任何本地伪造或改写。
   ========================================================================== */
'use strict';

/* ---------------------------------------------------------------- 工具函数 */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 转义后插入文本节点，避免任何 XSS 与结构破坏 */
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function fmtNum(n) {
  if (n === null || n === undefined || n === '') return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return v >= 1000 ? v.toLocaleString('zh-CN') : String(v);
}

function fmtScore(v, digits = 4) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : null;
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1024 / 1024).toFixed(2)} MB`;
}

async function api(path, options = {}) {
  const opt = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (opt.body && typeof opt.body !== 'string') opt.body = JSON.stringify(opt.body);
  const resp = await fetch(path, opt);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 空响应体 */ }
  if (!resp.ok) {
    const detail = data && (data.detail || data.message) ? data.detail : `HTTP ${resp.status}`;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = resp.status;
    throw err;
  }
  return data;
}

/** 往状态容器里写一条提示 */
function setStatus(container, kind, message, opts = {}) {
  if (!container) return;
  container.innerHTML = '';
  if (!message) return;
  if (kind === 'loading') {
    container.append(el('div', { class: 'loading' },
      el('span', { class: 'spinner' }), el('span', { text: message })));
    return;
  }
  const alert = el('div', { class: `alert alert-${kind}` },
    el('span', { class: 'ico', text: opts.icon || ({ ok: '✓', info: 'ℹ', warn: '!', err: '✕' }[kind] || 'ℹ') }),
    el('div', { class: 'body', html: message }));
  container.append(alert);
  if (opts.ttl) setTimeout(() => { if (container.firstChild === alert) container.innerHTML = ''; }, opts.ttl);
}

/* ------------------------------------------------------------ SQL 语法着色 */
const SQL_KEYWORDS = new Set([
  'SELECT','FROM','WHERE','AND','OR','NOT','IN','AS','ON','JOIN','LEFT','RIGHT','FULL','INNER','OUTER',
  'ORDER','BY','GROUP','HAVING','LIMIT','OFFSET','WITH','DISTINCT','DESC','ASC','INSERT','INTO','VALUES',
  'UPDATE','SET','DELETE','CREATE','TABLE','INDEX','IF','EXISTS','PRIMARY','KEY','FULLTEXT','VECTOR',
  'UNIQUE','NULL','IS','ISNULL','COALESCE','IFNULL','CASE','WHEN','THEN','ELSE','END','OVER','PARTITION',
  'ROW_NUMBER','RANK','DENSE_RANK','APPROXIMATE','UNION','ALL','RECURSIVE','PARSER','WITHIN','BETWEEN',
  'LIKE','MATCH','AGAINST','NATURAL','LANGUAGE','MODE','DATETIME','BIGINT','VARCHAR','TEXT','CHAR','INT',
  'DOUBLE','FLOAT','DECIMAL','DEFAULT','COMMENT','ENGINE','USING','ANALYZE','EXPLAIN','DROP','ALTER',
]);

/** 把一段 SQL 着色成安全的 HTML（先转义，再包 span） */
function colorSQL(sql) {
  if (!sql) return '';
  const src = String(sql);
  let out = '';
  let i = 0;
  const n = src.length;
  while (i < n) {
    const ch = src[i];
    // 行注释
    if (ch === '-' && src[i + 1] === '-') {
      const j = src.indexOf('\n', i);
      const end = j < 0 ? n : j;
      out += `<span class="cmt">${esc(src.slice(i, end))}</span>`;
      i = end;
      continue;
    }
    // 块注释
    if (ch === '/' && src[i + 1] === '*') {
      const j = src.indexOf('*/', i + 2);
      const end = j < 0 ? n : j + 2;
      out += `<span class="cmt">${esc(src.slice(i, end))}</span>`;
      i = end;
      continue;
    }
    // 字符串
    if (ch === "'" || ch === '"') {
      let j = i + 1;
      while (j < n) {
        if (src[j] === '\\') { j += 2; continue; }
        if (src[j] === ch) { if (src[j + 1] === ch) { j += 2; continue; } break; }
        j++;
      }
      const end = Math.min(j + 1, n);
      out += `<span class="str">${esc(src.slice(i, end))}</span>`;
      i = end;
      continue;
    }
    // 数字
    if (/[0-9]/.test(ch) && !/[A-Za-z0-9_]/.test(src[i - 1] || '')) {
      let j = i;
      while (j < n && /[0-9.eE+\-]/.test(src[j]) && !(src[j] === 'e' && j + 1 >= n)) j++;
      out += `<span class="num2">${esc(src.slice(i, j))}</span>`;
      i = j;
      continue;
    }
    // 标识符 / 关键字
    if (/[A-Za-z_]/.test(ch)) {
      let j = i;
      while (j < n && /[A-Za-z0-9_$.]/.test(src[j])) j++;
      const word = src.slice(i, j);
      const bare = word.split('.').pop().toUpperCase();
      if (SQL_KEYWORDS.has(bare) && !word.includes('.')) out += `<span class="kw">${esc(word)}</span>`;
      else if (src[j] === '(') out += `<span class="fn">${esc(word)}</span>`;
      else out += esc(word);
      i = j;
      continue;
    }
    out += esc(ch);
    i++;
  }
  return out;
}

/* ------------------------------------------------------ 命中词高亮（安全） */
/**
 * matched_terms 由后端给出（都是查询串里真实出现、且在正文里能找到的词元）。
 * 这里只做「大小写不敏感的子串包裹」，先转义再包裹，绝不注入原始 HTML。
 */
function highlight(content, terms) {
  const text = String(content || '');
  if (!terms || !terms.length) return esc(snippetTrim(text));
  const uniq = Array.from(new Set(terms.map(t => String(t).trim()).filter(t => t.length >= 2)))
    .sort((a, b) => b.length - a.length);
  if (!uniq.length) return esc(snippetTrim(text));

  const pattern = uniq.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  let re;
  try { re = new RegExp(pattern, 'gi'); } catch (_) { return esc(snippetTrim(text)); }

  const safe = snippetTrim(text);
  let out = '';
  let last = 0;
  let m;
  let guard = 0;
  while ((m = re.exec(safe)) !== null && guard++ < 400) {
    if (m.index > last) out += esc(safe.slice(last, m.index));
    out += `<mark>${esc(m[0])}</mark>`;
    last = m.index + m[0].length;
    if (m[0].length === 0) re.lastIndex++;
  }
  out += esc(safe.slice(last));
  return out;
}

/** 长正文取前若干字，尾部加省略号（在转义前做，保证不切断标记） */
function snippetTrim(text, max = 460) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length > max ? `${t.slice(0, max)}…` : t;
}

/* ==========================================================================
   全局状态
   ========================================================================== */
const state = {
  health: null,
  mode: 'hybrid',
  fusion: 'rrf',
  weight: 0.6,
  rankConstant: 60,
  topK: 8,
  kinds: new Set(),
  langs: new Set(),
  docFilter: [],
  presets: [],
  activePreset: null,
  lastResult: null,
  busy: false,
};

const MODE_HINT = {
  hybrid: '向量与全文两路同时下推到 seekdb，再由数据库融合排名。既能跨措辞找到近义表述，也不会漏掉精确术语。',
  semantic: '只走 VECTOR 索引（cosine + HNSW，ORDER BY … APPROXIMATE）。适合问法和原文用词不一致的场景。',
  keyword: '只走 FULLTEXT 索引（ik 中文分词 + BM25）。适合缩写、参数名、公式符号这类必须精确命中的词元。',
};

const KIND_LABEL = { paper: '论文', note: '笔记', record: '实验记录', unknown: '其他' };
const LANG_LABEL = { zh: '中文', en: '英文' };

/* ==========================================================================
   顶栏徽章
   ========================================================================== */
function renderBadges(h) {
  const box = $('#badges');
  if (!box) return;
  box.innerHTML = '';
  if (!h) {
    box.append(el('span', { class: 'badge badge-err', text: '后端不可达' }));
    return;
  }
  const reachable = !!h.seekdb_reachable;
  box.append(el('span', {
    class: `badge ${reachable ? 'badge-ok' : 'badge-err'}`,
    text: reachable ? `seekdb ${String(h.seekdb_version || '').replace(/^[\d.]+-?/, '') || '已连接'}` : 'seekdb 未连接',
    title: h.seekdb_version || '',
  }));
  const c = h.corpus || {};
  box.append(el('span', {
    class: `badge ${c.chunks ? 'badge-muted' : 'badge-warn'}`,
    text: `${fmtNum(c.docs)} 篇 / ${fmtNum(c.chunks)} 段`,
  }));
  const e = h.embedder || {};
  box.append(el('span', {
    class: `badge ${e.fitted ? 'badge-muted' : 'badge-warn'}`,
    text: `${e.backend || 'embedder'} · ${e.dim || '—'}d${e.fitted ? '' : ' · 未拟合'}`,
    title: e.method || '',
  }));
  if (h.status && h.status !== 'ok') {
    box.append(el('span', { class: 'badge badge-warn', text: '降级运行' }));
  }
}

/* ==========================================================================
   标签页
   ========================================================================== */
function initTabs() {
  $$('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.tab').forEach(b => {
        const on = b === btn;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      $$('.panel').forEach(p => p.classList.toggle('is-active', p.id === btn.dataset.panel));
      const panel = btn.dataset.panel;
      if (panel === 'panel-library') loadLibrary();
      if (panel === 'panel-about') loadAbout();
      if (panel === 'panel-ingest') loadPreview();
      try { history.replaceState(null, '', `#${panel.replace('panel-', '')}`); } catch (_) {}
    });
  });
  // 支持 #compare 这类锚点直接进入
  const hash = (location.hash || '').replace('#', '');
  if (hash) {
    const target = $(`.tab[data-panel="panel-${hash}"]`);
    if (target) target.click();
  }
}

/* ==========================================================================
   控制轨
   ========================================================================== */
function initControls() {
  // 检索模式
  $$('#mode-seg button').forEach(b => b.addEventListener('click', () => {
    state.mode = b.dataset.mode;
    $$('#mode-seg button').forEach(x => {
      const on = x === b;
      x.classList.toggle('is-active', on);
      x.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    $('#hybrid-controls').hidden = state.mode !== 'hybrid';
    $('#mode-hint').textContent = MODE_HINT[state.mode];
    paintResultModes();
  }));
  $('#mode-hint').textContent = MODE_HINT.hybrid;

  // 融合策略
  $$('#fusion-seg button').forEach(b => b.addEventListener('click', () => {
    state.fusion = b.dataset.fusion;
    $$('#fusion-seg button').forEach(x => {
      const on = x === b;
      x.classList.toggle('is-active', on);
      x.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    $('#weight-field').hidden = state.fusion !== 'weighted';
    $('#rc-field').hidden = state.fusion !== 'rrf';
  }));

  // 滑杆
  const bindRange = (id, outId, fmt, onInput) => {
    const input = $(id), out = $(outId);
    if (!input) return;
    const sync = () => { out.textContent = fmt(input.value); if (onInput) onInput(input.value); };
    input.addEventListener('input', sync);
    sync();
  };
  bindRange('#weight', '#weight-out', v => Number(v).toFixed(2), v => { state.weight = Number(v); });
  bindRange('#rank-constant', '#rc-out', v => String(v), v => { state.rankConstant = Number(v); });
  bindRange('#topk', '#topk-out', v => String(v), v => { state.topK = Number(v); });

  // 过滤器
  $$('#kind-checks input').forEach(cb => cb.addEventListener('change', () => {
    cb.checked ? state.kinds.add(cb.value) : state.kinds.delete(cb.value);
  }));
  $$('#lang-checks input').forEach(cb => cb.addEventListener('change', () => {
    cb.checked ? state.langs.add(cb.value) : state.langs.delete(cb.value);
  }));

  // 检索表单
  $('#search-form').addEventListener('submit', ev => {
    ev.preventDefault();
    doSearch($('#q').value);
  });
  $('#q').addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { ev.target.value = ''; }
  });

  // 对比表单
  $('#compare-form').addEventListener('submit', ev => {
    ev.preventDefault();
    doCompare($('#cq').value);
  });

  // Ctrl/Cmd + K 聚焦搜索框
  document.addEventListener('keydown', ev => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k') {
      ev.preventDefault();
      const tab = $('.tab[data-panel="panel-search"]');
      if (tab && !tab.classList.contains('is-active')) tab.click();
      $('#q').focus();
      $('#q').select();
    }
  });
}

function currentFilters() {
  return {
    kinds: Array.from(state.kinds),
    langs: Array.from(state.langs),
    doc_ids: Array.from(state.docFilter || []),
    tag: '',
    page_from: null,
    page_to: null,
  };
}

function filtersActive(f) {
  if (!f) return false;
  return (f.kinds && f.kinds.length) || (f.langs && f.langs.length) ||
         (f.doc_ids && f.doc_ids.length) || f.tag ||
         f.page_from !== null && f.page_from !== undefined ||
         f.page_to !== null && f.page_to !== undefined;
}

/* ==========================================================================
   预设查询
   ========================================================================== */
async function loadPresets() {
  const box = $('#presets');
  if (!box) return;
  try {
    const data = await api('/api/preset-queries');
    state.presets = data.queries || [];
  } catch (_) {
    state.presets = [];
  }
  box.innerHTML = '';
  if (!state.presets.length) return;
  box.append(el('span', { class: 'hint', style: 'align-self:center;margin-right:2px', text: '试试这些真实问题：' }));
  state.presets.forEach(p => {
    const text = p.text || p.query || p;
    const intent = p.intent || '';
    box.append(el('button', {
      class: 'chip', type: 'button', title: intent ? `${text}\n\n${intent}` : text,
      text: String(text),
      onclick: ev => {
        $$('#presets .chip').forEach(c => c.classList.remove('is-active'));
        ev.currentTarget.classList.add('is-active');
        state.activePreset = intent;
        $('#q').value = String(text);
        doSearch(String(text));
      },
    }));
  });
}

/* ==========================================================================
   检索
   ========================================================================== */
async function doSearch(qtext) {
  const q = String(qtext || '').trim();
  const statusBox = $('#results-status');
  const list = $('#results');
  if (!q) { setStatus(statusBox, 'warn', '请输入检索词'); $('#q').focus(); return; }
  if (state.busy) return;
  state.busy = true;

  const btn = $('#search-form button[type="submit"]');
  if (btn) btn.disabled = true;
  setStatus(statusBox, 'loading', `正在向 seekdb 下推${state.mode === 'hybrid' ? '向量 + 全文两路' : (state.mode === 'semantic' ? '向量' : '全文')}检索…`);
  list.innerHTML = '';

  try {
    const res = await api('/api/search', {
      method: 'POST',
      body: {
        query: q, mode: state.mode, fusion: state.fusion,
        semantic_weight: state.weight, top_k: state.topK,
        candidate_k: Math.max(60, state.topK * 6),
        rank_constant: state.rankConstant,
        filters: currentFilters(), log: true,
      },
    });
    state.lastResult = res;
    renderResults(res);
    renderLastMeta(res);
    setStatus(statusBox, '', '');
    if (res.error) {
      setStatus(statusBox, 'warn',
        `本次使用了降级执行路径：<code>${esc(res.execution)}</code><br>${esc(res.error)}`);
    }
  } catch (err) {
    setStatus(statusBox, 'err', `检索失败：${esc(err.message)}`);
    renderEmpty(list, '检索没有返回结果', err.message);
  } finally {
    state.busy = false;
    if (btn) btn.disabled = false;
  }
}

function renderEmpty(container, title, desc) {
  container.innerHTML = '';
  container.append(el('li', { class: 'empty', style: 'list-style:none' },
    el('span', { class: 'glyph', text: '溯' }),
    el('h4', { text: title }),
    desc ? el('p', { text: desc }) : null));
}

function renderResults(res) {
  const list = $('#results');
  list.innerHTML = '';
  const hits = res.hits || [];
  if (!hits.length) {
    renderEmpty(list, '没有命中任何段落',
      filtersActive(res.filters)
        ? '当前的结构化过滤条件太窄了，试着取消一两个过滤项。'
        : '试试更短的问法，或切到「关键词」模式看全文索引能否命中。');
    return;
  }
  hits.forEach(h => list.append(renderHit(h, res)));
}

function renderHit(h, res) {
  const mode = res.mode === 'hybrid' ? 'hybrid' : res.mode;
  const node = el('li', { class: 'hit', dataset: { mode, chunkId: h.chunk_id } });

  /* --- 顶部：名次 + 标题 + 分数 --- */
  const top = el('div', { class: 'hit-top' },
    el('span', { class: 'rank', text: `#${h.final_rank || '—'}` }),
    el('span', { class: 'hit-title', text: h.doc_title || h.source_path || `chunk ${h.chunk_id}` }));

  const scores = el('div', { class: 'scores' });
  const fused = fmtScore(h.fused !== undefined ? h.fused : h.rrf_score);
  if (fused !== null) {
    scores.append(el('span', {
      class: 'score s-fused',
      text: `${res.mode === 'hybrid' && res.fusion === 'rrf' ? 'RRF' : '融合'} ${fused}`,
      title: res.mode === 'hybrid' && res.fusion === 'rrf'
        ? 'seekdb 内部 RRF 融合分数（dbms_hybrid_search 返回的 _score）'
        : '归一化后按语义权重加权得到的融合分数',
    }));
  }
  const vs = fmtScore(h.vec_score);
  scores.append(el('span', {
    class: `score s-vec${vs === null ? ' s-na' : ''}`,
    text: vs === null ? '语义 —' : `语义 ${vs}${h.vec_rank ? ` ·r${h.vec_rank}` : ''}`,
    title: '1 − cosine_distance，由 VECTOR 索引（HNSW）计算；r 为语义分支内排名',
  }));
  const bm = fmtScore(h.bm25);
  scores.append(el('span', {
    class: `score s-bm25${bm === null ? ' s-na' : ''}`,
    text: bm === null ? 'BM25 —' : `BM25 ${bm}${h.bm25_rank ? ` ·r${h.bm25_rank}` : ''}`,
    title: 'MATCH … AGAINST 在 FULLTEXT 索引（ik 分词）上的相关度；r 为关键词分支内排名',
  }));
  top.append(scores);
  node.append(top);

  /* --- 出处路径 --- */
  const path = el('div', { class: 'hit-path' });
  path.append(el('span', { class: 'kind-tag', dataset: { kind: h.doc_kind || 'unknown' },
    text: KIND_LABEL[h.doc_kind] || h.doc_kind || '—' }));
  if (h.lang) path.append(el('span', { text: LANG_LABEL[h.lang] || h.lang }));
  if (h.section_path) path.append(el('span', { class: 'section-path', text: h.section_path }));
  if (h.page_no !== null && h.page_no !== undefined && h.page_no !== '') {
    path.append(el('span', { class: 'sep', text: '·' }), el('span', { text: `第 ${h.page_no} 页` }));
  }
  if (h.line_start) {
    path.append(el('span', { class: 'sep', text: '·' }),
      el('span', { text: `行 ${h.line_start}${h.line_end && h.line_end !== h.line_start ? `–${h.line_end}` : ''}` }));
  }
  path.append(el('span', { class: 'sep', text: '·' }),
    el('code', { class: 'truncate', text: fileName(h.source_path), title: h.source_path || '' }));
  node.append(path);

  /* --- 正文摘要 + 命中词高亮 --- */
  node.append(el('p', { class: 'snippet', html: highlight(h.content, h.matched_terms) }));

  /* --- 底部操作 --- */
  const foot = el('div', { class: 'hit-foot' });
  foot.append(el('button', { class: 'mini', type: 'button', text: '查看 SQL',
    onclick: () => openDrawer(res) }));
  foot.append(el('button', { class: 'mini', type: 'button', text: '以段找段',
    title: '用这个段落自己存的向量做近邻检索，复用同一个 VECTOR 索引',
    onclick: () => openRelated(h) }));
  foot.append(el('button', { class: 'mini', type: 'button', text: '展开全文',
    onclick: ev => toggleFull(ev.currentTarget, h) }));
  foot.append(el('span', { class: 'grow' }));
  if (h.matched_terms && h.matched_terms.length) {
    foot.append(el('span', { class: 'hash', text: `命中 ${h.matched_terms.length} 个查询词元` }));
  }
  foot.append(el('span', { class: 'hash', text: `chunk_id ${h.chunk_id}` }));
  node.append(foot);

  return node;
}

function fileName(p) {
  if (!p) return '—';
  const parts = String(p).split(/[\\/]/);
  return parts[parts.length - 1] || String(p);
}

async function toggleFull(btn, h) {
  const hit = btn.closest('.hit');
  const snip = $('.snippet', hit);
  if (!snip) return;
  if (btn.dataset.expanded === '1') {
    snip.innerHTML = highlight(h.content, h.matched_terms);
    btn.dataset.expanded = '0';
    btn.textContent = '展开全文';
    return;
  }
  btn.disabled = true;
  btn.textContent = '读取中…';
  try {
    // 完整正文直接回表取，保证展示的是 seekdb 里存的原文
    const full = await api(`/api/chunks/${h.chunk_id}`);
    snip.textContent = '';
    snip.style.whiteSpace = 'pre-wrap';
    snip.textContent = full.content || h.content || '';
    btn.dataset.expanded = '1';
    btn.textContent = '收起';
  } catch (err) {
    snip.textContent = String(h.content || '');
    btn.textContent = '展开全文';
  } finally {
    btn.disabled = false;
  }
}

function paintResultModes() {
  $$('#results .hit').forEach(node => { node.dataset.mode = state.mode === 'hybrid' ? 'hybrid' : state.mode; });
}

function renderLastMeta(res) {
  const box = $('#last-meta');
  if (!box) return;
  box.innerHTML = '';
  const rows = [
    ['执行方式', res.execution],
    ['耗时', `${res.elapsed_ms} ms`],
    ['返回', `${res.n_results} 段`],
    ['候选池', res.candidate_k],
  ];
  if (res.mode === 'hybrid' && res.fusion === 'weighted') rows.splice(1, 0, ['语义权重', Number(res.semantic_weight).toFixed(2)]);
  if (res.mode === 'hybrid' && res.fusion === 'rrf') rows.splice(1, 0, ['rank_constant', res.rank_constant]);
  if (filtersActive(res.filters)) rows.push(['过滤', describeFilters(res.filters)]);
  rows.forEach(([k, v]) => box.append(el('div', {}, el('b', { text: `${k} ` }), document.createTextNode(String(v)))));
  box.append(el('button', { class: 'mini', style: 'margin-top:6px;align-self:flex-start', type: 'button',
    text: '查看本次 SQL', onclick: () => openDrawer(res) }));
}

function describeFilters(f) {
  const bits = [];
  if (f.kinds && f.kinds.length) bits.push(f.kinds.map(k => KIND_LABEL[k] || k).join('/'));
  if (f.langs && f.langs.length) bits.push(f.langs.map(k => LANG_LABEL[k] || k).join('/'));
  if (f.doc_ids && f.doc_ids.length) bits.push(`${f.doc_ids.length} 篇文档`);
  if (f.tag) bits.push(`tag:${f.tag}`);
  if (f.page_from !== null && f.page_from !== undefined) bits.push(`页≥${f.page_from}`);
  if (f.page_to !== null && f.page_to !== undefined) bits.push(`页≤${f.page_to}`);
  return bits.join('，') || '无';
}

/* ==========================================================================
   SQL 抽屉
   ========================================================================== */
function openDrawer(res, opts = {}) {
  const drawer = $('#drawer'), backdrop = $('#drawer-backdrop');
  const body = $('#drawer-sql'), extra = $('#drawer-extra'), note = $('#drawer-note');
  $('#drawer-title').textContent = opts.title || '本次检索实际执行的 SQL';

  const sqls = (res.sql || []).filter(s => s && String(s).trim());
  note.innerHTML = opts.note || explainExecution(res);
  body.innerHTML = sqls.length
    ? sqls.map(s => colorSQL(s)).join('\n\n-- ---- 下一条 ----\n\n')
    : '<span class="cmt">（本次没有返回 SQL 文本）</span>';

  extra.innerHTML = '';
  if (res.native_params) {
    extra.append(el('h4', { text: 'dbms_hybrid_search.search() 的入参 DSL（ES 兼容）' }));
    extra.append(el('pre', { class: 'code', text: JSON.stringify(res.native_params, null, 2) }));
    extra.append(el('p', { class: 'hint', style: 'margin-top:8px',
      text: '上面的 SQL 不是本项目手写的，而是 seekdb 收到这段 DSL 后自己生成的；用 dbms_hybrid_search.get_sql() 原样取回。' }));
  }
  if (opts.extraHTML) extra.append(el('div', { html: opts.extraHTML }));

  drawer.hidden = false;
  backdrop.hidden = false;
  document.body.style.overflow = 'hidden';
  $('#drawer-close').focus();
}

function explainExecution(res) {
  const map = {
    'semantic': '纯语义检索：VECTOR 索引 + <code>cosine_distance</code> + <code>ORDER BY … APPROXIMATE</code>（HNSW 近邻）。',
    'keyword': '纯关键词检索：FULLTEXT 索引（ik 中文分词）+ <code>MATCH … AGAINST</code>（BM25）。',
    'hybrid/native-rrf': '混合检索：调用 seekdb <strong>原生</strong> <code>dbms_hybrid_search</code>，两路召回、FULL JOIN、RANK() 与 RRF 融合全部在数据库内部完成。',
    'hybrid/weighted-single-sql': '混合检索：在<strong>一条 SQL</strong> 内完成两路召回 → 各自按最大值归一化 → 加权融合 → 排序，融合发生在数据库里。',
    'hybrid/weighted-two-pass': '混合检索：当前 seekdb 版本不接受上面那种单条 SQL 写法，已自动降级为两条下推 SQL（分数仍由 seekdb 计算），归一化与加权在应用层完成。',
  };
  let s = map[res.execution] || `执行方式：<code>${esc(res.execution || '')}</code>`;
  s += ` 实测耗时 <strong>${res.elapsed_ms} ms</strong>，返回 ${res.n_results} 段。`;
  return s;
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('#drawer-backdrop').hidden = true;
  if ($('#modal').hidden) document.body.style.overflow = '';
}

/* ==========================================================================
   相关段落弹层
   ========================================================================== */
async function openRelated(h) {
  const modal = $('#modal'), backdrop = $('#modal-backdrop'), body = $('#modal-body');
  $('#modal-title').textContent = `以段找段 · 从「${(h.doc_title || '').slice(0, 28) || h.chunk_id}」出发`;
  body.innerHTML = '';
  body.append(el('div', { class: 'loading' }, el('span', { class: 'spinner' }),
    el('span', { text: '用该段落自己存的向量在 VECTOR 索引上做近邻检索…' })));
  modal.hidden = false; backdrop.hidden = false;
  document.body.style.overflow = 'hidden';
  try {
    const data = await api(`/api/chunks/${h.chunk_id}/related?k=6`);
    body.innerHTML = '';
    const hits = data.hits || [];
    if (!hits.length) {
      body.append(el('div', { class: 'empty' }, el('h4', { text: '没有找到相关段落' })));
    } else {
      body.append(el('p', { class: 'hint', style: 'margin-bottom:10px',
        html: `复用同一张 <code>chunk</code> 表上的 VECTOR 索引，不需要额外建模：<br>` }));
      const list = el('ol', { class: 'result-list', style: 'list-style:none;padding:0;margin:0' });
      hits.forEach((r, idx) => {
        list.append(el('li', { class: 'hit', dataset: { mode: 'semantic' } },
          el('div', { class: 'hit-top' },
            el('span', { class: 'rank', text: `#${idx + 1}` }),
            el('span', { class: 'hit-title', text: r.doc_title || r.source_path || '' }),
            el('div', { class: 'scores' },
              el('span', { class: 'score s-vec', text: `cos ${fmtScore(r.vec_score)}` }))),
          el('div', { class: 'hit-path' },
            r.section_path ? el('span', { class: 'section-path', text: r.section_path }) : null,
            r.page_no ? el('span', { text: `第 ${r.page_no} 页` }) : null,
            el('code', { text: fileName(r.source_path) })),
          el('p', { class: 'snippet', html: highlight(r.content, []) })));
      });
      body.append(list);
      body.append(el('div', { style: 'margin-top:12px' },
        el('button', { class: 'mini', type: 'button', text: '查看这条近邻 SQL',
          onclick: () => openDrawer({ sql: data.sql, execution: 'semantic', elapsed_ms: '—', n_results: hits.length },
            { title: '以段找段的近邻 SQL', note: '直接把该分块已存储的 <code>emb</code> 作为查询向量，省掉一次向量化。' }) })));
    }
  } catch (err) {
    body.innerHTML = '';
    body.append(el('div', { class: 'alert alert-err' }, el('span', { class: 'ico', text: '✕' }),
      el('div', { class: 'body', text: err.message })));
  }
}

function closeModal() {
  $('#modal').hidden = true;
  $('#modal-backdrop').hidden = true;
  if ($('#drawer').hidden) document.body.style.overflow = '';
}

/* ==========================================================================
   三路对比
   ========================================================================== */
async function doCompare(qtext) {
  const q = String(qtext || '').trim();
  const statusBox = $('#compare-status');
  if (!q) { setStatus(statusBox, 'warn', '请输入检索词'); $('#cq').focus(); return; }

  const btn = $('#compare-form button[type="submit"]');
  if (btn) btn.disabled = true;
  setStatus(statusBox, 'loading', '同一个问题正在分别走纯语义 / 纯关键词 / seekdb 原生混合三条路径…');
  $('#compare-summary').innerHTML = '';
  $('#compare-table tbody').innerHTML = '';

  try {
    const res = await api('/api/compare', {
      method: 'POST',
      body: { query: q, top_k: Math.max(8, state.topK), filters: currentFilters() },
    });
    renderCompare(res);
    setStatus(statusBox, '', '');
  } catch (err) {
    setStatus(statusBox, 'err', `对比失败：${esc(err.message)}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderCompare(res) {
  const a = res.agreement || {};
  const box = $('#compare-summary');
  box.innerHTML = '';
  const cards = [
    { cls: 'm-both', v: a.semantic_and_keyword, k: '两路都召回', d: '语义与关键词都认可的核心段落' },
    { cls: 'm-sem',  v: a.only_semantic, k: '只有语义找到', d: '换了措辞、字面不重合，靠向量召回' },
    { cls: 'm-kw',   v: a.only_keyword, k: '只有关键词命中', d: '精确术语/缩写，向量容易漏' },
    { cls: 'm-both', v: a.hybrid_vs_semantic_overlap, k: '混合 ∩ 语义', d: `${res.modes.hybrid.execution || ''}` },
    { cls: 'm-kw',   v: a.hybrid_vs_keyword_overlap, k: '混合 ∩ 关键词', d: 'RRF 对两路的取舍结果' },
  ];
  cards.forEach(c => box.append(el('div', { class: `metric ${c.cls}` },
    el('div', { class: 'v', text: fmtNum(c.v) }),
    el('div', { class: 'k', text: c.k }),
    el('div', { class: 'd', text: c.d }))));

  const ms = res.modes || {};
  box.append(el('div', { class: 'metric' },
    el('div', { class: 'v', style: 'font-size:15px', text:
      `${ms.semantic ? ms.semantic.elapsed_ms : '—'} / ${ms.keyword ? ms.keyword.elapsed_ms : '—'} / ${ms.hybrid ? ms.hybrid.elapsed_ms : '—'} ms` }),
    el('div', { class: 'k', text: '语义 / 关键词 / 混合 耗时' }),
    el('div', { class: 'd', text: '全部由 seekdb 执行' })));

  const tbody = $('#compare-table tbody');
  tbody.innerHTML = '';
  const rows = res.rows || [];
  if (!rows.length) {
    tbody.append(el('tr', {}, el('td', { colspan: '6', class: 'muted', text: '三路都没有召回结果' })));
  }
  rows.forEach(r => {
    const cover = { both: '两路共有', semantic_only: '仅语义', keyword_only: '仅关键词', none: '仅混合' }[r.coverage] || r.coverage;
    tbody.append(el('tr', {},
      el('td', { class: 'snippet-cell', title: r.snippet || '', text: snippetTrim(r.snippet, 90) }),
      el('td', {},
        el('div', { class: 'title-cell', text: r.doc_title || '' }),
        el('div', { class: 'path-cell', text:
          `${r.section_path ? r.section_path + ' · ' : ''}${r.page_no ? '第 ' + r.page_no + ' 页 · ' : ''}${fileName(r.source_path)}` })),
      rankCell(r.semantic_rank), rankCell(r.keyword_rank), rankCell(r.hybrid_rank, true),
      el('td', {}, el('span', { class: 'coverage', dataset: { c: r.coverage }, text: cover }))));
  });

  // 三路 SQL 一并可查
  const sqls = [].concat(ms.semantic ? ms.semantic.sql : [], ms.keyword ? ms.keyword.sql : [], ms.hybrid ? ms.hybrid.sql : []);
  const foot = $('#compare-foot');
  if (foot) foot.remove();
  const wrap = el('div', { id: 'compare-foot', style: 'margin-top:12px' },
    el('button', { class: 'btn', type: 'button', text: '查看三路各自的 SQL',
      onclick: () => openDrawer({ sql: sqls, execution: 'compare', elapsed_ms: '—', n_results: rows.length },
        { title: '三路对比实际执行的 SQL', note: '三条路径分别下推到 seekdb，可以在这里逐条核对语法差异。' }) }));
  $('#panel-compare').append(wrap);
}

function rankCell(v, strong = false) {
  if (v === null || v === undefined) {
    return el('td', { class: 'num' }, el('span', { class: 'rank-cell is-miss', text: '—' }));
  }
  return el('td', { class: 'num' },
    el('span', { class: 'rank-cell is-hit', style: strong ? 'font-size:13.5px' : '', text: String(v) }));
}

/* ==========================================================================
   文档库
   ========================================================================== */
async function loadLibrary() {
  const statusBox = $('#library-status');
  const tbody = $('#library-table tbody');
  setStatus(statusBox, 'loading', '从 seekdb 的 doc 表读取文档清单…');
  try {
    const data = await api('/api/documents');
    setStatus(statusBox, '', '');
    renderLibraryStats(data.stats || {});
    tbody.innerHTML = '';
    const docs = data.documents || [];
    if (!docs.length) {
      tbody.append(el('tr', {}, el('td', { colspan: '7' },
        el('div', { class: 'empty', style: 'border:0;padding:24px' },
          el('h4', { text: '文档库是空的' }),
          el('p', { text: '去「导入」页扫描语料目录并建立索引。' })))));
      return;
    }
    docs.forEach(d => {
      tbody.append(el('tr', {},
        el('td', { class: 'title-cell' },
          el('div', { text: d.title || '（无标题）' }),
          d.tags ? el('div', { class: 'path-cell', text: d.tags }) : null),
        el('td', {}, el('span', { class: 'kind-tag', dataset: { kind: d.doc_kind || 'unknown' },
          text: KIND_LABEL[d.doc_kind] || d.doc_kind || '—' })),
        el('td', { class: 'muted', text: LANG_LABEL[d.lang] || d.lang || '—' }),
        el('td', { class: 'num', text: fmtNum(d.n_chunks) }),
        el('td', { class: 'num', text: fmtNum(d.n_chars) }),
        el('td', { class: 'path-cell', title: d.source_path || '', text: fileName(d.source_path) }),
        el('td', { class: 'actions' },
          el('button', { class: 'mini', type: 'button', text: '检索这篇',
            onclick: () => searchWithin(d) }),
          ' ',
          el('button', { class: 'mini btn-danger', type: 'button', text: '移除',
            title: '从 seekdb 删除这篇文档及其所有分块',
            onclick: () => removeDoc(d) }))));
    });
  } catch (err) {
    setStatus(statusBox, 'err', `读取文档库失败：${esc(err.message)}`);
  }
}

function renderLibraryStats(c) {
  const box = $('#library-stats');
  box.innerHTML = '';
  const kinds = (c.by_kind || []).map(k => `${KIND_LABEL[k.doc_kind] || k.doc_kind} ${k.docs}篇/${k.chunks}段`).join('　');
  const langs = (c.by_lang || []).map(k => `${LANG_LABEL[k.lang] || k.lang || '—'} ${k.chunks}段`).join('　');
  [
    { v: fmtNum(c.docs), k: '文档', d: kinds || '—' },
    { v: fmtNum(c.chunks), k: '段落分块', d: langs || '—' },
    { v: fmtNum(c.chars), k: '字符总量', d: '按目标长度 520 字滑窗切分，保留 80 字重叠' },
  ].forEach(m => box.append(el('div', { class: 'metric' },
    el('div', { class: 'v', text: m.v }), el('div', { class: 'k', text: m.k }),
    el('div', { class: 'd', text: m.d }))));
}

/** 「检索这篇」：把 doc_id 过滤加到检索页，然后让用户直接提问 */
function searchWithin(d) {
  const tab = $('.tab[data-panel="panel-search"]');
  if (tab) tab.click();
  state.docFilter = [d.doc_id];
  const q = $('#q');
  q.value = '';
  q.placeholder = `只在《${(d.title || '').slice(0, 30) || d.doc_id}》里检索…（点右侧提示可取消）`;
  q.focus();
  setStatus($('#results-status'), 'info',
    `已把检索范围限定到 <code>${esc(d.title || d.doc_id)}</code>（doc_id 过滤，与向量/全文在同一条 SQL 内下推）。` +
    ` <a href="#" id="clear-doc-filter">取消限定</a>`);
  const link = $('#clear-doc-filter');
  if (link) link.addEventListener('click', ev => {
    ev.preventDefault();
    state.docFilter = [];
    q.placeholder = '用一句话描述你要找的内容，例如：持续小幅慢漂移用哪种方法在线检测';
    setStatus($('#results-status'), '', '');
    renderFirstScreen();
  });
}

async function removeDoc(d) {
  if (!confirm(`确认从 seekdb 删除《${d.title || d.doc_id}》及其 ${d.n_chunks || 0} 个分块？`)) return;
  try {
    const res = await api(`/api/documents/${encodeURIComponent(d.doc_id)}`, { method: 'DELETE' });
    setStatus($('#library-status'), 'ok', `已删除，共移除 ${res.deleted_chunks} 个分块。`, { ttl: 4000 });
    loadLibrary();
    loadHealth();
  } catch (err) {
    setStatus($('#library-status'), 'err', `删除失败：${esc(err.message)}`);
  }
}

/* ==========================================================================
   导入
   ========================================================================== */
async function loadPreview() {
  const statusBox = $('#ingest-status');
  const tbody = $('#preview-table tbody');
  setStatus(statusBox, 'loading', '扫描语料目录并计算 SHA-256 指纹…');
  try {
    const data = await api('/api/ingest/preview');
    setStatus(statusBox, 'info',
      `语料目录 <code>${esc(data.corpus_dir)}</code>，发现 ${(data.files || []).length} 个可解析文件。`);
    tbody.innerHTML = '';
    (data.files || []).forEach(f => {
      tbody.append(el('tr', {},
        el('td', { class: 'path-cell', title: f.rel, text: f.rel }),
        el('td', {}, el('span', { class: 'kind-tag', dataset: { kind: f.kind || 'unknown' },
          text: KIND_LABEL[f.kind] || f.kind || '—' })),
        el('td', { class: 'num', text: fmtBytes(f.size) }),
        el('td', {}, f.already_imported
          ? el('span', { class: 'coverage', dataset: { c: 'both' }, text: '已导入' })
          : el('span', { class: 'coverage', dataset: { c: 'keyword_only' }, text: '待导入' }))));
    });
    if (!(data.files || []).length) {
      tbody.append(el('tr', {}, el('td', { colspan: '4', class: 'muted', text: '语料目录里没有可解析的文件' })));
    }
  } catch (err) {
    setStatus(statusBox, 'err', `扫描失败：${esc(err.message)}`);
  }
}

async function runIngest(mode) {
  const statusBox = $('#ingest-status');
  const report = $('#ingest-report');
  const btns = $$('#panel-ingest .btn');
  btns.forEach(b => { b.disabled = true; });
  setStatus(statusBox, 'loading', mode === 'rebuild'
    ? '全量重建：解析全部文件 → 在语料上拟合 LSA → 向量化 → 写入 seekdb（VECTOR + FULLTEXT 索引会随之重建）…'
    : '增量导入：只处理指纹未见过的新文件…');
  report.innerHTML = '';
  const t0 = Date.now();
  try {
    const res = await api('/api/ingest', { method: 'POST', body: { mode, limit: null } });
    const secs = ((Date.now() - t0) / 1000).toFixed(1);
    setStatus(statusBox, 'ok',
      `导入完成（${mode === 'rebuild' ? '全量重建' : '增量'}，墙钟 ${secs}s）：` +
      `${res.files_imported} 个文件写入，${res.files_skipped} 个跳过，共 <strong>${fmtNum(res.chunks)}</strong> 个分块、${fmtNum(res.chars)} 字符。`);
    renderIngestReport(res);
    loadPreview();
    loadHealth();
  } catch (err) {
    setStatus(statusBox, 'err', `导入失败：${esc(err.message)}`);
  } finally {
    btns.forEach(b => { b.disabled = false; });
  }
}

function renderIngestReport(res) {
  const box = $('#ingest-report');
  box.innerHTML = '';
  const card = el('div', { class: 'report-card' });
  card.append(el('h4', { text: `导入报告 · ${res.mode === 'rebuild' ? '全量重建' : '增量导入'}` }));

  const emb = res.embedder || {};
  const kv = el('dl', { class: 'kv' });
  [
    ['向量维度', `${res.dim} d`],
    ['向量化后端', [emb.backend, emb.method].filter(Boolean).join(' · ') || '—'],
    ['词表规模', fmtNum(emb.vocab_size)],
    ['文件（导入/跳过/总计）', `${res.files_imported} / ${res.files_skipped} / ${res.files_total}`],
    ['分块数', fmtNum(res.chunks)],
    ['字符数', fmtNum(res.chars)],
  ].forEach(([k, v]) => { kv.append(el('dt', { text: k }), el('dd', { text: v })); });
  card.append(kv);

  const t = res.timings || {};
  const max = Math.max(t.parse_s || 0, t.embed_s || 0, t.db_s || 0, 0.001);
  const bars = el('div', { class: 'timing-bars' });
  [['解析分块', t.parse_s], ['向量化', t.embed_s], ['写入 seekdb', t.db_s], ['合计', t.total_s]].forEach(([lab, v]) => {
    const val = Number(v) || 0;
    bars.append(el('div', { class: 'timing-row' },
      el('span', { class: 'lab', text: lab }),
      el('span', { class: 'bar' }, el('i', { style: `width:${Math.max(2, (val / max) * 100).toFixed(1)}%` })),
      el('span', { class: 'val', text: `${val.toFixed(2)} s` })));
  });
  card.append(bars);

  const per = res.per_file || [];
  if (per.length) {
    card.append(el('h4', { style: 'margin-top:14px', text: `逐文件明细（${per.length}）` }));
    const wrap = el('div', { class: 'table-wrap', style: 'max-height:280px' });
    const tb = el('table', { class: 'grid' });
    tb.append(el('thead', {}, el('tr', {},
      el('th', { text: '文件' }), el('th', { text: '类型' }), el('th', { text: '语言' }),
      el('th', { class: 'num', text: '分块' }), el('th', { class: 'num', text: '字符' }),
      el('th', { class: 'num', text: '解析 s' }))));
    const tbody = el('tbody');
    per.forEach(f => tbody.append(el('tr', {},
      el('td', { class: 'path-cell', title: f.file || '', text: f.file || fileName(f.source_path) }),
      el('td', {}, el('span', { class: 'kind-tag', dataset: { kind: f.kind || 'unknown' },
        text: KIND_LABEL[f.kind] || f.kind || '—' })),
      el('td', { class: 'muted', text: LANG_LABEL[f.lang] || f.lang || '—' }),
      el('td', { class: 'num', text: fmtNum(f.chunks) }),
      el('td', { class: 'num', text: fmtNum(f.chars) }),
      el('td', { class: 'num', text: f.parse_seconds !== undefined ? Number(f.parse_seconds).toFixed(2) : '—' }))));
    tb.append(tbody);
    wrap.append(tb);
    card.append(wrap);
  }

  const errs = res.errors || [];
  if (errs.length) {
    card.append(el('h4', { style: 'margin-top:14px;color:var(--err)', text: `错误（${errs.length}）` }));
    const ul = el('ul', { class: 'err-list' });
    errs.forEach(e => ul.append(el('li', { text: typeof e === 'string' ? e : JSON.stringify(e) })));
    card.append(ul);
  }
  box.append(card);
}

/* ==========================================================================
   运行环境
   ========================================================================== */
async function loadAbout() {
  const statusBox = $('#about-status');
  setStatus(statusBox, 'loading', '读取 seekdb 实例信息、表结构与检索日志…');
  try {
    const h = state.health || await api('/api/health');
    state.health = h;
    renderBadges(h);
    const cfg = h.config || {};
    const sb = cfg.seekdb || {};
    const chunking = cfg.chunking || {};
    const search_ = cfg.search || {};

    fillKV($('#about-seekdb'), [
      ['版本', h.seekdb_version || '—'],
      ['连接', `${sb.host || '127.0.0.1'}:${sb.port || ''}`],
      ['库名', sb.database || '—'],
      ['账号', sb.user || '—'],
      ['向量索引', 'VECTOR(emb) · DISTANCE=COSINE · TYPE=HNSW'],
      ['全文索引', 'FULLTEXT(content, section_path) · PARSER ik'],
      ['原生混合检索', search_.native_hybrid_enabled ? 'dbms_hybrid_search 已启用' : '未启用'],
      ['状态', h.seekdb_reachable ? '在线' : '不可达'],
    ], { 状态: h.seekdb_reachable ? 'ok' : 'warn' });

    const e = h.embedder || {};
    fillKV($('#about-embedder'), [
      ['后端', e.backend || '—'],
      ['方法', e.method || '—'],
      ['维度', `${e.dim || '—'} d`],
      ['词表规模', fmtNum(e.vocab_size)],
      ['拟合语料', `${fmtNum(e.n_docs_fit)} 段`],
      ['最大奇异值', e.top_singular_value ? Number(e.top_singular_value).toFixed(3) : '—'],
      ['分词器', e.jieba ? 'jieba' : 'CJK 二元组回退'],
      ['已拟合', e.fitted ? '是' : '否（需先执行导入）'],
    ], { 已拟合: e.fitted ? 'ok' : 'warn' });

    const c = h.corpus || {};
    fillKV($('#about-corpus'), [
      ['文档数', fmtNum(c.docs)],
      ['分块数', fmtNum(c.chunks)],
      ['字符总量', fmtNum(c.chars)],
      ['按类型', (c.by_kind || []).map(k => `${KIND_LABEL[k.doc_kind] || k.doc_kind} ${k.docs}`).join('，') || '—'],
      ['按语言', (c.by_lang || []).map(k => `${LANG_LABEL[k.lang] || k.lang || '—'} ${k.chunks}`).join('，') || '—'],
      ['语料目录', cfg.corpus_dir || '—'],
      ['分块策略', `目标 ${chunking.target_chars} / 上限 ${chunking.max_chars} / 重叠 ${chunking.overlap_chars} 字`],
      ['默认参数', `top_k=${search_.top_k}　candidate_k=${search_.candidate_k}　RRF k=${search_.rrf_k}`],
    ]);

    const ddl = h.chunk_table_ddl;
    $('#about-ddl').innerHTML = ddl ? colorSQL(ddl) : '<span class="cmt">（表尚未创建，先执行一次导入）</span>';

    const logs = await api('/api/logs?limit=25');
    renderLogs(logs.logs || []);
    setStatus(statusBox, '', '');
  } catch (err) {
    setStatus(statusBox, 'err', `读取运行环境失败：${esc(err.message)}`);
  }
}

function fillKV(node, pairs, cls = {}) {
  if (!node) return;
  node.innerHTML = '';
  pairs.forEach(([k, v]) => {
    node.append(el('dt', { text: k }));
    node.append(el('dd', { class: cls[k] || '', text: v }));
  });
}

function renderLogs(logs) {
  const tbody = $('#log-table tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (!logs.length) {
    tbody.append(el('tr', {}, el('td', { colspan: '6', class: 'muted', text: 'search_log 表还没有记录，去检索页发起一次查询即可写入。' })));
    return;
  }
  logs.forEach(l => tbody.append(el('tr', {},
    el('td', { class: 'nowrap mono', style: 'font-size:11px', text: String(l.created_at || '').replace('T', ' ').slice(0, 19) }),
    el('td', { class: 'title-cell', text: l.query_text || '' }),
    el('td', { text: ({ hybrid: '混合', semantic: '语义', keyword: '关键词' })[l.mode] || l.mode || '—' }),
    el('td', { class: 'mono', style: 'font-size:11px', text: l.fusion ? `${l.fusion}${l.weight !== null && l.weight !== undefined ? ' w=' + Number(l.weight).toFixed(2) : ''}` : '—' }),
    el('td', { class: 'num', text: fmtNum(l.n_results) }),
    el('td', { class: 'num', text: l.elapsed_ms !== null && l.elapsed_ms !== undefined ? Number(l.elapsed_ms).toFixed(1) : '—' }))));
}

/* ==========================================================================
   健康检查
   ========================================================================== */
async function loadHealth() {
  try {
    const h = await api('/api/health');
    state.health = h;
    renderBadges(h);
    if ($('#panel-about').classList.contains('is-active')) loadAbout();
    return h;
  } catch (err) {
    state.health = null;
    renderBadges(null);
    setStatus($('#results-status'), 'err',
      `无法连接后端 API：<code>${esc(err.message)}</code><br>` +
      '请先在服务器上启动服务：<code>.venv/bin/python -m uvicorn app.api:app --host 0.0.0.0 --port 8090</code>');
    return null;
  }
}

/* ==========================================================================
   启动
   ========================================================================== */
function initOverlays() {
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-backdrop').addEventListener('click', closeDrawer);
  $('#modal-close').addEventListener('click', closeModal);
  $('#modal-backdrop').addEventListener('click', closeModal);
  document.addEventListener('keydown', ev => {
    if (ev.key !== 'Escape') return;
    if (!$('#modal').hidden) closeModal();
    else if (!$('#drawer').hidden) closeDrawer();
  });
}

function initIngestButtons() {
  $('#btn-preview').addEventListener('click', loadPreview);
  $('#btn-incremental').addEventListener('click', () => runIngest('incremental'));
  $('#btn-rebuild').addEventListener('click', () => {
    if (confirm('全量重建会清空 chunk / doc 表并在整个语料上重新拟合向量模型，确定继续？')) runIngest('rebuild');
  });
}

function renderFirstScreen() {
  const list = $('#results');
  list.innerHTML = '';
  list.append(el('li', { class: 'empty', style: 'list-style:none' },
    el('span', { class: 'glyph', text: '溯' }),
    el('h4', { text: '输入一句话开始检索' }),
    el('p', { html:
      '上面几个胶囊是这套语料里的真实问题，点一下就能跑。<br>' +
      '每一条结果都带着 seekdb 算出的语义分数与 BM25 分数，' +
      '点「查看 SQL」可以看到这次检索在数据库里<strong>实际执行</strong>的语句。' }),
    el('p', { class: 'hint', style: 'margin-top:10px', text: '快捷键：Ctrl/⌘ + K 聚焦检索框' })));
}

async function boot() {
  initTabs();
  initControls();
  initOverlays();
  initIngestButtons();
  renderFirstScreen();

  const t = $('#foot-time');
  const tick = () => { if (t) t.textContent = new Date().toLocaleString('zh-CN', { hour12: false }); };
  tick(); setInterval(tick, 1000);

  await Promise.all([loadHealth(), loadPresets()]);

  // 语料为空时，直接把用户引导到导入页
  const c = (state.health && state.health.corpus) || {};
  if (state.health && state.health.seekdb_reachable && !c.chunks) {
    const list = $('#results');
    list.innerHTML = '';
    list.append(el('li', { class: 'empty', style: 'list-style:none' },
      el('span', { class: 'glyph', text: '空' }),
      el('h4', { text: '索引是空的' }),
      el('p', { html:
        'seekdb 已经连上，但 <code>chunk</code> 表里还没有数据。<br>' +
        '先去「导入」页扫描语料目录并执行一次全量重建，索引建好后这里就能检索了。' }),
      el('div', { style: 'margin-top:14px' },
        el('button', { class: 'btn btn-primary', type: 'button', text: '去导入语料',
          onclick: () => { const t = $('.tab[data-panel="panel-ingest"]'); if (t) t.click(); } }))));
    return;
  }

  applyDeepLink();
}


/**
 * 深链：?q=…&mode=…&fusion=…&w=…&rc=…&topk=…&kind=…&lang=…&tab=…
 *
 * 两个用途：
 *   1. 评审/同事可以直接分享一条「带着参数和结果」的链接，复现某次检索；
 *   2. 无头浏览器截图时可以靠它把界面驱动到指定状态。
 * 全部通过模拟真实点击/输入事件生效，保证和手动操作走同一条代码路径。
 */
function applyDeepLink() {
  const p = new URLSearchParams(location.search);
  const setRange = (id, val) => {
    const r = document.getElementById(id);
    if (!r || val === null || val === '') return;
    r.value = String(val);
    r.dispatchEvent(new Event('input', { bubbles: true }));
  };
  const tab = p.get('tab');
  if (tab) {
    const t = $(`.tab[data-panel="panel-${tab}"]`);
    if (t) t.click();
  }
  const mode = p.get('mode');
  if (mode) { const b = $(`#mode-seg button[data-mode="${mode}"]`); if (b) b.click(); }
  const fusion = p.get('fusion');
  if (fusion) { const b = $(`#fusion-seg button[data-fusion="${fusion}"]`); if (b) b.click(); }
  setRange('weight', p.get('w'));
  setRange('rank-constant', p.get('rc'));
  setRange('topk', p.get('topk'));
  const toggleChecks = (boxId, csv) => {
    (csv || '').split(',').map(s => s.trim()).filter(Boolean).forEach(v => {
      const cb = $(`#${boxId} input[value="${v}"]`);
      if (cb && !cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
    });
  };
  toggleChecks('kind-checks', p.get('kind'));
  toggleChecks('lang-checks', p.get('lang'));

  const q = (p.get('q') || '').trim();
  if (q) {
    const input = $('#q');
    if (input) input.value = q;
    if (tab === 'compare') {
      // 对比面板有自己的输入框与请求，深链同样驱动它，保证截图/分享链接落在真实结果上
      const cq = $('#cq');
      if (cq) cq.value = q;
      doCompare(q);
    } else {
      doSearch(q);
    }
  }
}

document.addEventListener('DOMContentLoaded', boot);
