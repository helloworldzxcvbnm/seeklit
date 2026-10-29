/* 溯文 SeekLit 站点离线演示。
 *
 * 数据来自 snapshot.json：2026-10-30 由服务器上的 seekdb 真实查询导出，
 * 候选池里每段都带原始 vsim（余弦相似度）与 bm25_raw（全文打分）。
 * 本脚本在浏览器里基于这些原始值重算四种检索路径的排序：
 *   semantic  = 按 vsim 降序
 *   keyword   = 按 bm25_raw 降序
 *   rrf       = 1/(语义排名+rc) + 1/(关键词排名+rc)，排名由快照原始分数在池内排序得到
 *   weighted  = w·vsim/max(vsim) + (1-w)·bm25/max(bm25)
 * 当权重恰好等于快照里预跑的 0/0.3/0.5/0.7/1.0 五档时，排序直接采用 seekdb 真实执行的
 * ids 顺序并打「真实执行」徽章——此时展示的就是数据库返回的顺序，不是浏览器算的。
 */
(function () {
  'use strict';

  var SNAP = null;
  var state = { qi: 0, mode: 'rrf', w: 0.6, rc: 60, k: 8 };

  function $(s) { return document.querySelector(s); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(x, n) { return Number(x || 0).toFixed(n == null ? 4 : n); }

  var MODE_LABEL = {
    semantic: '语义路（VECTOR + cosine + HNSW）',
    keyword: '关键词路（FULLTEXT + ik + BM25）',
    rrf: '混合 · seekdb 原生 RRF（数据库内融合）',
    weighted: '混合 · 单 SQL 可调权重（归一化 + 加权）'
  };

  function load() {
    return fetch('snapshot.json').then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function poolRanks(pool) {
    var sem = pool.slice().sort(function (a, b) { return (b.vsim || 0) - (a.vsim || 0); });
    var kw = pool.slice().sort(function (a, b) { return (b.bm25_raw || 0) - (a.bm25_raw || 0); });
    var sr = {}, kr = {};
    sem.forEach(function (e, i) { sr[e.chunk_id] = i + 1; });
    kw.forEach(function (e, i) { kr[e.chunk_id] = i + 1; });
    var vmax = sem.length ? (sem[0].vsim || 0) : 0;
    var fmax = kw.length ? (kw[0].bm25_raw || 0) : 0;
    return { sr: sr, kr: kr, vmax: vmax, fmax: fmax };
  }

  function compute(q) {
    var pool = q.pool, R = poolRanks(pool), out = [], real = false, elapsed = null, execution = '';
    var i, e;
    function mapIds(ids) {
      var byId = {};
      pool.forEach(function (x) { byId[x.chunk_id] = x; });
      return (ids || []).map(function (id) { return byId[id]; }).filter(Boolean);
    }
    if (state.mode === 'semantic') {
      execution = q.results.semantic.execution; elapsed = q.results.semantic.elapsed_ms;
      if (q.results.semantic.ids && q.results.semantic.ids.length >= state.k) {
        out = mapIds(q.results.semantic.ids); real = true;
      } else {
        out = pool.slice().sort(function (a, b) { return (b.vsim || 0) - (a.vsim || 0); });
      }
      out.forEach(function (x) { x._score = x.vsim || 0; x._bar = Math.max(0, Math.min(1, x._score)); });
    } else if (state.mode === 'keyword') {
      execution = q.results.keyword.execution; elapsed = q.results.keyword.elapsed_ms;
      if (q.results.keyword.ids && q.results.keyword.ids.length >= state.k) {
        out = mapIds(q.results.keyword.ids); real = true;
      } else {
        out = pool.slice().sort(function (a, b) { return (b.bm25_raw || 0) - (a.bm25_raw || 0); });
      }
      out.forEach(function (x) { x._score = x.bm25_raw || 0; x._bar = R.fmax ? x._score / R.fmax : 0; });
    } else if (state.mode === 'rrf') {
      execution = q.results.hybrid_rrf.execution; elapsed = q.results.hybrid_rrf.elapsed_ms;
      // 快照里的原生 RRF 结果就是 rank_constant=60 跑出来的；不动滑块时直接展示真实顺序
      if (state.rc === 60 && q.results.hybrid_rrf.ids && q.results.hybrid_rrf.ids.length >= state.k) {
        out = mapIds(q.results.hybrid_rrf.ids); real = true;
      } else {
        out = pool.slice();
      }
      out.forEach(function (x) {
        x._score = 1 / ((R.sr[x.chunk_id] || 9999) + state.rc) + 1 / ((R.kr[x.chunk_id] || 9999) + state.rc);
      });
      var mx = 0;
      out.forEach(function (x) { mx = Math.max(mx, x._score); });
      out.forEach(function (x) { x._bar = mx ? x._score / mx : 0; });
      if (!real) out.sort(function (a, b) { return b._score - a._score; });
    } else {
      var variant = null;
      q.results.hybrid_weighted.forEach(function (v) {
        if (Math.abs(v.semantic_weight - state.w) < 1e-9) variant = v;
      });
      if (variant) {
        real = true; elapsed = variant.elapsed_ms; execution = variant.execution;
        var byId = {};
        pool.forEach(function (x) { byId[x.chunk_id] = x; });
        variant.ids.forEach(function (id) { if (byId[id]) out.push(byId[id]); });
        pool.forEach(function (x) { if (out.indexOf(x) < 0 && out.length < state.k) out.push(x); });
      } else {
        out = pool.slice();
        execution = 'browser-refuse（权重档未预跑，浏览器基于快照原始分数重算）';
      }
      out.forEach(function (x) {
        var vn = R.vmax ? (x.vsim || 0) / R.vmax : 0;
        var bn = R.fmax ? (x.bm25_raw || 0) / R.fmax : 0;
        x._score = state.w * vn + (1 - state.w) * bn;
        x._bar = Math.max(0, Math.min(1, x._score));
      });
      if (!real) out.sort(function (a, b) { return b._score - a._score; });
    }
    return { hits: out.slice(0, state.k), real: real, elapsed: elapsed, execution: execution, R: R };
  }

  function highlight(text, terms) {
    var safe = esc(text);
    (terms || []).forEach(function (t) {
      if (!t || t.length < 1) return;
      var re = new RegExp(esc(esc(t)), 'gi');
      safe = safe.replace(re, function (m) { return '<mark>' + m + '</mark>'; });
    });
    return safe;
  }

  function renderHits(res, q) {
    var ol = $('#d-hits');
    ol.innerHTML = '';
    res.hits.forEach(function (h, i) {
      var li = document.createElement('li');
      var chips = '';
      if (state.mode === 'semantic') chips = '<span class="chip v">vsim ' + fmt(h.vsim) + '</span>';
      else if (state.mode === 'keyword') chips = '<span class="chip b">bm25 ' + fmt(h.bm25_raw, 3) + '</span>';
      else chips = '<span class="chip f">融合 ' + fmt(h._score) + '</span>' +
                   '<span class="chip v">语义 ' + fmt(h.vsim) + '</span>' +
                   '<span class="chip b">BM25 ' + fmt(h.bm25_raw, 3) + '</span>';
      var loc = [h.doc_kind, h.lang === 'zh' ? '中文' : (h.lang === 'en' ? '英文' : h.lang),
                 h.section_path || '', h.line_start ? '行 ' + h.line_start + '-' + h.line_end : '',
                 h.source_path || ''].filter(Boolean).map(esc).join(' · ');
      li.innerHTML =
        '<div class="h-top"><span class="rank">#' + (i + 1) + '</span>' +
        '<span class="ttl">' + esc(h.doc_title) + '</span>' +
        '<span class="scores">' + chips + '</span></div>' +
        '<div class="meta">' + loc + ' · chunk_id ' + h.chunk_id + '</div>' +
        '<p class="body">' + highlight(h.content, state.mode === 'keyword' ? h.matched_terms : null) + '</p>' +
        '<div class="bar"><i style="width:' + Math.round((h._bar || 0) * 100) + '%"></i></div>';
      ol.appendChild(li);
    });
  }

  function renderMeta(res, q) {
    var dl = $('#d-meta');
    var rows = [
      ['执行方式', res.execution],
      ['排序来源', res.real ? 'seekdb 真实执行顺序' : '浏览器基于快照原始分数重算'],
      ['seekdb 耗时', res.elapsed == null ? '—' : fmt(res.elapsed, 1) + ' ms'],
      ['候选池', q.pool.length + ' 段（两路候选并集）'],
      ['top_k', state.k],
      ['语义权重', state.mode === 'weighted' ? fmt(state.w, 2) : '—'],
      ['rank_constant', state.mode === 'rrf' ? state.rc : '—']
    ];
    dl.innerHTML = rows.map(function (r) {
      return '<dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd>';
    }).join('');

    var sub = $('#d-sub');
    sub.innerHTML = '问题意图：' + esc(q.intent || '—') + '　|　当前路径：' + esc(MODE_LABEL[state.mode]) +
      '　|　<b>' + (res.real ? '真实执行' : '浏览器重算') + '</b>';
  }

  function renderVariants(q) {
    var box = $('#d-variants');
    box.innerHTML = '';
    q.results.hybrid_weighted.forEach(function (v) {
      var b = document.createElement('button');
      b.textContent = 'w=' + v.semantic_weight.toFixed(1);
      if (state.mode === 'weighted' && Math.abs(v.semantic_weight - state.w) < 1e-9) b.className = 'on';
      b.addEventListener('click', function () {
        state.mode = 'weighted';
        state.w = v.semantic_weight;
        $('#d-w').value = String(v.semantic_weight);
        syncSeg(); syncOut(); render();
      });
      box.appendChild(b);
    });
  }

  function renderCompare(q) {
    var rows = q.compare.rows || [];
    var cov = { both: '两路共有', semantic_only: '仅语义', keyword_only: '仅关键词', none: '—' };
    var html = '<table><thead><tr><th>语义</th><th>关键词</th><th>混合</th><th>覆盖</th><th>段落</th></tr></thead><tbody>';
    rows.slice(0, 9).forEach(function (r) {
      html += '<tr><td>' + (r.semantic_rank || '–') + '</td><td>' + (r.keyword_rank || '–') + '</td><td>' +
        (r.hybrid_rank || '–') + '</td><td>' + esc(cov[r.coverage] || r.coverage) + '</td><td>' +
        esc((r.doc_title || '') + ' / ' + (r.section_path || '')).slice(0, 46) + '</td></tr>';
    });
    $('#d-compare').innerHTML = html + '</tbody></table>';
  }

  function renderSql(q) {
    var pre = $('#d-sql'), head = '';
    if (state.mode === 'semantic') { head = q.results.semantic.execution; pre.textContent = q.results.semantic.sql; }
    else if (state.mode === 'keyword') { head = q.results.keyword.execution; pre.textContent = q.results.keyword.sql; }
    else if (state.mode === 'rrf') {
      head = q.results.hybrid_rrf.execution;
      pre.textContent = '-- 应用侧 DSL 的 rank 参数: ' +
        JSON.stringify(q.results.hybrid_rrf.native_params || {}) +
        '\n-- 下面是 seekdb 自己生成的 SQL（dbms_hybrid_search.get_sql）\n' + q.results.hybrid_rrf.sql;
    } else {
      var best = q.results.hybrid_weighted[0];
      q.results.hybrid_weighted.forEach(function (v) {
        if (Math.abs(v.semantic_weight - state.w) < Math.abs(best.semantic_weight - state.w)) best = v;
      });
      head = best.execution + ' @ w=' + best.semantic_weight;
      pre.textContent = '-- 展示最接近当前权重(' + state.w.toFixed(2) + ')的真实执行 SQL：w=' +
        best.semantic_weight + '，elapsed ' + best.elapsed_ms + ' ms\n' + best.sql;
    }
    $('#d-sqlbox summary').textContent = '查看本次 SQL（' + head + '）';
  }

  function syncSeg() {
    document.querySelectorAll('#d-mode button').forEach(function (b) {
      b.classList.toggle('on', b.dataset.mode === state.mode);
    });
  }
  function syncOut() {
    $('#d-w-out').textContent = Number(state.w).toFixed(2);
    $('#d-rc-out').textContent = state.rc;
  }

  function render() {
    if (!SNAP) return;
    var q = SNAP.queries[state.qi];
    var res = compute(q);
    renderHits(res, q);
    renderMeta(res, q);
    renderVariants(q);
    renderCompare(q);
    renderSql(q);
  }

  function boot() {
    load().then(function (snap) {
      SNAP = snap;
      var sel = $('#d-q');
      snap.queries.forEach(function (q, i) {
        var o = document.createElement('option');
        o.value = String(i);
        o.textContent = q.text;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () { state.qi = Number(sel.value); render(); });
      document.querySelectorAll('#d-mode button').forEach(function (b) {
        b.addEventListener('click', function () { state.mode = b.dataset.mode; syncSeg(); render(); });
      });
      $('#d-w').addEventListener('input', function (ev) { state.w = Number(ev.target.value); syncOut(); render(); });
      $('#d-rc').addEventListener('input', function (ev) { state.rc = Number(ev.target.value); syncOut(); render(); });
      $('#d-k').addEventListener('change', function (ev) { state.k = Number(ev.target.value); render(); });
      syncSeg(); syncOut(); render();
    }).catch(function (err) {
      $('#d-sub').innerHTML = '快照加载失败：' + esc(err.message) +
        '。请用静态服务器打开本页（例如 python -m http.server），直接双击文件时浏览器会禁止 fetch 本地 JSON。';
    });
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
