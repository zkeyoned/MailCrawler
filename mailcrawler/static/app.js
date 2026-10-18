/* 邮件爬虫控制台 前端
 * 所有邮件文本一律用 textContent 插入（createElement + textContent），不使用 innerHTML 拼接邮件内容。
 * 20 只蜘蛛由同一个 requestAnimationFrame 循环驱动，画在同一块 canvas 上。
 */
'use strict';
(() => {
  // ------------------------------------------------------------ 小工具
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const fmt = n => Number(n || 0).toLocaleString('en-US');
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const CAT_ORDER = ['coursework', 'academic_admin', 'campus_life', 'event', 'career', 'newsletter', 'personal'];
  const CAT_COLOR = {
    coursework: '#ff6b6b', academic_admin: '#ffa94d', campus_life: '#69db7c', event: '#4dabf7',
    career: '#da77f2', newsletter: '#7d8799', personal: '#ffd43b'
  };
  const CAT_ZH = {
    coursework: '课程作业考试', academic_admin: '教务行政', campus_life: '校园生活与服务',
    event: '讲座活动比赛', career: '实习招聘', newsletter: '群发通知与订阅', personal: '写给你本人'
  };
  const DDL_ZH = { expired: '已过期', open: '还没过', none: '无截止' };

  async function api(path, opt) {
    const r = await fetch(path, opt);
    let j = null;
    try { j = await r.json(); } catch (e) { /* 非 JSON */ }
    if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
    return j;
  }
  const post = (p, body) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  let toastTimer = 0;
  function toast(msg, isErr) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'show' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = ''; }, 3200);
  }

  // ------------------------------------------------------------ 全局状态
  const S = { view: 'crawl', grid: 9, speed: 1, q: '', stats: null, prog: null, setup: null };
  const Q = { items: [], fetching: false, last: 0, exhausted: false };   // 爬虫取信队列
    const W = { total: 0, pending: 0, inited: false };                     // WORDS READ
  const GRIDS = { 4: [2, 2], 9: [3, 3], 12: [4, 3], 20: [5, 4] };        // [列, 行]
  const FS = { 4: 15, 9: 13, 12: 12, 20: 10.5 };                         // 字号随格子数缩小
  const BASE_PX = 320;       // 1× 速度，像素/秒
  const MAX_UNITS = 400;     // 每封最多爬多少个字/词

  // ------------------------------------------------------------ 数字滚动
  class Odo {
    constructor(host) { this.host = host; this.str = ''; this.cols = []; this.box = host.closest('.ctr'); }
    set(n) {
      const s = fmt(n);
      if (s === this.str) return;
      const rebuild = s.length !== this.str.length;
      const first = this.str === '';
      this.str = s;
      if (rebuild) {
        this.host.replaceChildren();
        this.cols = [];
        for (const ch of s) {
          if (ch === ',') { this.host.append(el('span', 'sep', ',')); this.cols.push(null); continue; }
          const dg = el('span', 'dg');
          const st = el('span', 'strip');
          for (let d = 0; d < 10; d++) st.append(el('span', 'n', String(d)));
          dg.append(st);
          this.host.append(dg);
          this.cols.push(st);
        }
        requestAnimationFrame(() => this.apply());
      } else {
        this.apply();
      }
      if (!first && this.box) {
        this.box.classList.remove('flash');
        void this.box.offsetWidth;
        this.box.classList.add('flash');
      }
    }
    apply() {
      for (let i = 0; i < this.cols.length; i++) {
        if (this.cols[i]) this.cols[i].style.transform = 'translateY(' + (-(+this.str[i])) + 'em)';
      }
    }
  }
  const odo = {};

  // ------------------------------------------------------------ 文本切分：中文按字、英文按单词
  const TOKEN_RE = /[㐀-鿿豈-﫿]|[A-Za-z0-9À-ɏ][A-Za-z0-9À-ɏ_'’\-.@:\/]*|\s+|[\s\S]/gu;
  const UNIT_RE = /^(?:[㐀-鿿豈-﫿]|[A-Za-z0-9À-ɏ])/u;
  function tokenize(text, maxUnits) {
    const out = [];
    let units = 0;
    const m = text.match(TOKEN_RE) || [];
    for (const t of m) {
      const isUnit = UNIT_RE.test(t);
      out.push({ t, unit: isUnit });
      if (isUnit && ++units >= maxUnits) { out.push({ t: ' …', unit: false }); break; }
    }
    return out;
  }
  function shortFrom(s) {
    s = (s || '').trim();
    const m = s.match(/^"?([^"<]+?)"?\s*</);
    if (m && m[1].trim()) return m[1].trim();
    return s.replace(/[<>]/g, '').split('@')[0] || '(未知发件人)';
  }
  function fmtDdl(dl) {   // '2026-10-16 17:00' -> '10月16日 17:00'
    const m = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?/.exec(dl || '');
    if (!m) return dl || '';
    const t = (m[4] && !(m[4] === '23' && m[5] === '59')) ? ' ' + m[4] + ':' + m[5] : '';
    return (+m[2]) + '月' + (+m[3]) + '日' + t;
  }

  // ------------------------------------------------------------ 蜘蛛绘制（火柴棍风格）
  function drawSpider(ctx, x, y, h, ph, sc, color, alpha) {
    const L = 7.6 * sc;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = Math.max(1.1, 1.15 * sc);
    const ch = Math.cos(h), sh = Math.sin(h);
    // 光晕
    ctx.globalAlpha = alpha * 0.16;
    ctx.beginPath(); ctx.arc(x, y, 11 * sc, 0, 6.2832); ctx.fill();
    ctx.globalAlpha = alpha;
    // 8 条腿：每侧 4 条，相邻腿反相摆动（交替步态）
    ctx.beginPath();
    for (let s = -1; s <= 1; s += 2) {
      for (let k = 0; k < 4; k++) {
        const along = (1.3 - k * 0.85) * sc;
        const rx = x + ch * along, ry = y + sh * along;
        const base = 0.62 + k * 0.5;
        const sw = Math.sin(ph + (k % 2 ? Math.PI : 0) + (s > 0 ? 0 : Math.PI));
        const a = h + s * (base + sw * 0.26);
        const reach = L * (1.12 + 0.1 * Math.cos(ph * 1 + k)) * (1 - 0.1 * Math.max(0, sw));
        const fx = rx + Math.cos(a) * reach, fy = ry + Math.sin(a) * reach;
        const ak = h + s * (1.5708 + (base - 1.5708) * 0.3);
        const kx = rx + Math.cos(ak) * reach * 0.58, ky = ry + Math.sin(ak) * reach * 0.58;
        ctx.moveTo(rx, ry); ctx.lineTo(kx, ky); ctx.lineTo(fx, fy);
      }
    }
    ctx.stroke();
    // 身体：腹部 + 胸 + 头
    ctx.beginPath(); ctx.ellipse(x - ch * 2.9 * sc, y - sh * 2.9 * sc, 3.5 * sc, 2.7 * sc, h, 0, 6.2832); ctx.fill();
    ctx.beginPath(); ctx.arc(x, y, 2.2 * sc, 0, 6.2832); ctx.fill();
    ctx.beginPath(); ctx.arc(x + ch * 2.9 * sc, y + sh * 2.9 * sc, 1.6 * sc, 0, 6.2832); ctx.fill();
    ctx.restore();
  }

  // ------------------------------------------------------------ 爬虫格子
  class Cell {
    constructor(i, fs) {
      this.i = i;
      this.color = 'hsl(' + ((i * 47 + 12) % 360) + ',88%,64%)';
      this.dead = false;
      const root = el('div', 'cell idle');
      root.style.setProperty('--c', this.color);
      root.style.setProperty('--fs', fs + 'px');
      this.fs = fs;
      this.root = root;
      this.stripe = el('div', 'stripe');
      const head = el('div', 'head');
      const r1 = el('div', 'row1');
      this.fromEl = el('span', 'from');
      this.dateEl = el('span', 'date');
      r1.append(this.fromEl, this.dateEl);
      this.subjEl = el('div', 'subj');
      head.append(r1, this.subjEl);
      this.body = el('div', 'body');
      this.txt = el('div', 'txt');
      this.body.append(this.txt);
      this.verdict = el('div', 'verdict');
      this.flash = el('div', 'flash', '→ JEV');
      this.msgEl = el('div', 'msg');
      root.append(this.stripe, head, this.body, this.verdict, this.flash, this.msgEl);
      this.sc = clamp(fs / 11, 0.95, 1.6);
      this.state = 'idle';
      this.msgText = '';
      this.reset();
    }
    reset() {
      this.state = 'idle';
      this.mail = null;
      this.units = [];
      this.idx = 0;
      this.sx = this.sy = 0;
      this.heading = 0;
      this.phase = Math.random() * 6;
      this.scroll = 0;
      this.maxScroll = 0;
      this.trail = [];
      this.t = 0;
      this.ox = this.oy = this.bw = this.bh = 0;
      this.root.classList.remove('fading', 'hasv', 'jev');
      this.root.classList.add('idle');
      this.root.style.removeProperty('--cat');
      this.stripe.style.background = 'transparent';
      this.fromEl.textContent = '';
      this.dateEl.textContent = '';
      this.subjEl.textContent = '';
      this.txt.replaceChildren();
      this.txt.style.transform = '';
      this.verdict.replaceChildren();
      this.setMsg('', false);
    }
    setMsg(t, err) {
      if (t === this.msgText && !err === !this.msgEl.classList.contains('err')) return;
      this.msgText = t;
      this.msgEl.textContent = t;
      this.msgEl.classList.toggle('err', !!err);
    }
    start(m) {
      this.mail = m;
      this.root.classList.remove('idle', 'fading', 'hasv', 'jev');
      this.root.style.removeProperty('--cat');
      this.stripe.style.background = 'transparent';
      this.verdict.replaceChildren();
      this.setMsg('', false);
      this.fromEl.textContent = shortFrom(m.sender);
      this.dateEl.textContent = (m.date || '').slice(0, 16);
      this.subjEl.textContent = m.subject || '(无主题)';
      const toks = tokenize((m.body || '').trim() || '（无正文）', MAX_UNITS);
      const frag = document.createDocumentFragment();
      this.units = [];
      for (const k of toks) {
        if (k.unit) {
          const s = el('span', 'u', k.t);
          frag.append(s);
          this.units.push({ el: s, x: 0, y: 0, cx: 0, cy: 0, w: 0, h: 0 });
        } else {
          frag.append(document.createTextNode(k.t));
        }
      }
      this.txt.replaceChildren(frag);
      this.txt.style.transform = '';
      this.scroll = 0;
      this.trail = [];
      this.idx = 0;
      this.state = 'crawl';
      this.layout();
      const u = this.units[0];
      if (u) { this.sx = Math.max(0, u.cx - 30); this.sy = u.cy; }
    }
    // 读取格子在舞台上的位置 + 每个字的坐标（一次性批量读，避免反复强制重排）
    layout() {
      const stage = Stage.el.getBoundingClientRect();
      const b = this.body.getBoundingClientRect();
      this.ox = b.left - stage.left + 9;   // 9 = body 左 padding
      this.oy = b.top - stage.top;
      this.bw = b.width - 18;
      this.bh = b.height;
      if (!this.units.length) return;
      const tops = this.units.map(u => [u.el.offsetLeft, u.el.offsetTop, u.el.offsetWidth, u.el.offsetHeight]);
      for (let i = 0; i < tops.length; i++) {
        const u = this.units[i], t = tops[i];
        u.x = t[0]; u.y = t[1]; u.w = t[2]; u.h = t[3];
        u.cx = t[0] + t[2] / 2; u.cy = t[1] + t[3] / 2;
      }
      this.maxScroll = Math.max(0, this.txt.scrollHeight - this.body.clientHeight);
    }
    update(dt) {
      this.phase += dt * (this.state === 'crawl' ? 16 * clamp(S.speed, 0.4, 2.2) : 3);
      if (this.state === 'idle') {
        const m = takeMail();
        if (m) { this.start(m); return; }
        let msg;
        if (!Q.exhausted) msg = '取信中…';
        else if (S.prog && S.prog.syncing) msg = '等待同步新邮件…';
        else if (S.stats && S.stats.total === 0) msg = '库是空的\n点左侧「＋ 新一轮爬取」开始同步邮件';
        else msg = '全部爬完 ✓';
        this.setMsg(msg, false);
        return;
      }
      if (this.state === 'crawl') this.crawl(dt);
      else if (this.state === 'result') {
        this.t += dt;
        if (this.t > 1.5 && !this.root.classList.contains('fading')) this.root.classList.add('fading');
        if (this.t > 1.98) {
          this.reset();
        }
      }
      // 滚动：让蜘蛛始终在可见区域里
      if (this.state === 'crawl' || this.state === 'jev') {
        const want = clamp(this.sy - this.bh * 0.42, 0, this.maxScroll);
        this.scroll += (want - this.scroll) * Math.min(1, dt * 6);
        const v = Math.round(this.scroll * 10) / 10;
        if (v !== this._sv) { this._sv = v; this.txt.style.transform = 'translateY(' + (-v) + 'px)'; }
      }
    }
    crawl(dt) {
      let budget = BASE_PX * S.speed * dt;
      let guard = 12;
      while (budget > 0 && guard-- > 0) {
        const u = this.units[this.idx];
        if (!u) { this.finishCrawl(); return; }
        const dx = u.cx - this.sx, dy = u.cy - this.sy;
        const dist = Math.hypot(dx, dy);
        const k = Math.abs(dy) > u.h * 0.8 ? 3 : 1;      // 换行时跑得快一点
        const step = budget * k;
        if (dist <= step + 0.5) {
          this.sx = u.cx; this.sy = u.cy;
          budget -= dist / k;
          this.readUnit(u);
          this.idx++;
        } else {
          if (dist > 0.01) this.heading = this.angleTo(Math.atan2(dy, dx), dt);
          this.sx += dx / dist * step; this.sy += dy / dist * step;
          budget = 0;
        }
      }
      const tr = this.trail, n = tr.length;
      if (!n || Math.hypot(this.sx - tr[n - 1][0], this.sy - tr[n - 1][1]) > 5) {
        tr.push([this.sx, this.sy]);
        if (tr.length > 70) tr.shift();
      }
      if (this.idx >= this.units.length) this.finishCrawl();
    }
    angleTo(target, dt) {   // 转向平滑
      let d = target - this.heading;
      while (d > Math.PI) d -= 6.2832;
      while (d < -Math.PI) d += 6.2832;
      return this.heading + d * Math.min(1, dt * 14);
    }
    readUnit(u) {
      u.el.classList.add('r');
      W.total++; W.pending++;
    }
    finishCrawl() {
      if (this.state !== 'crawl') return;
      this.state = 'jev';
      this.root.classList.add('jev');
      const uid = this.mail.uid;
      post('/api/classify/' + uid).then(r => this.onResult(r), e => this.onError(e));
    }
    onResult(r) {
      if (this.dead) return;
      const col = CAT_COLOR[r.cat] || '#8896ab';
      this.root.style.setProperty('--cat', col);
      this.stripe.style.background = col;
      this.root.classList.remove('jev');
      this.root.classList.add('hasv');
      const row1 = el('div', 'vrow');
      row1.append(el('i', 'vdot'), el('b', '', r.cat_zh || r.cat), el('span', 'conf', Math.round((r.cat_conf || 0) * 100) + '%'));
      const row2 = el('div', 'vrow');
      row2.append(el('span', '', '📅'));
      if (r.deadline) row2.append(el('span', '', fmtDdl(r.deadline)));
      row2.append(el('span', 'badge b-' + r.ddl_status, DDL_ZH[r.ddl_status] || r.ddl_status));
      const row3 = el('div', 'vrow');
      const act = (r.action || 0) >= 0.6;
      row3.append(el('span', 'badge ' + (act ? 'b-todo' : 'b-none'), act ? '✋ 要动手' : '不用动手'),
        el('span', 'conf', Math.round((r.action || 0) * 100) + '%'));
      this.verdict.replaceChildren(row1, row2, row3);
      this.state = 'result';
      this.t = 0;
      scheduleRefresh();
    }
    onError(e) {
      if (this.dead) return;
      this.root.classList.remove('jev');
      this.setMsg('Jev 出错：' + (e && e.message ? e.message : e), true);
      this.state = 'result';
      this.t = 0;
      scheduleRefresh();
    }
    draw(ctx) {
      if (this.state !== 'crawl' && this.state !== 'jev') return;
      ctx.save();
      ctx.beginPath();
      ctx.rect(this.ox, this.oy, this.bw, this.bh);
      ctx.clip();
      const px = this.ox, py = this.oy - this.scroll;
      const tr = this.trail, n = tr.length;
      if (n > 2) {   // 拖着的细丝
        ctx.lineWidth = 1;
        ctx.strokeStyle = this.color;
        for (let band = 0; band < 3; band++) {
          const a = Math.floor(n * band / 3), b = Math.min(n - 1, Math.floor(n * (band + 1) / 3) + 1);
          ctx.globalAlpha = 0.14 + band * 0.16;
          ctx.beginPath();
          ctx.moveTo(px + tr[a][0], py + tr[a][1]);
          for (let j = a + 1; j <= b; j++) ctx.lineTo(px + tr[j][0], py + tr[j][1]);
          ctx.lineTo(px + this.sx, py + this.sy);
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
      }
      drawSpider(ctx, px + this.sx, py + this.sy, this.heading, this.phase, this.sc, this.color, 1);
      ctx.restore();
    }
  }

  // ------------------------------------------------------------ 取信队列
  function takeMail() {
    while (Q.items.length) {
      const m = Q.items.shift();
      if (!isActive(m.uid)) return m;
    }
    refill();
    return null;
  }
  function isActive(uid) {   // 已经在某个格子里爬
    for (const c of Stage.cells) if (c.mail && c.mail.uid === uid && c.state !== 'idle') return true;
    return false;
  }
  async function refill() {
    if (Q.fetching) return;
    const now = performance.now();
    const gap = Q.exhausted ? ((S.prog && S.prog.syncing) ? 1500 : 6000) : 250;
    if (Q.last && now - Q.last < gap) return;
    Q.fetching = true;
    Q.last = now;
    try {
      const r = await api('/api/next?n=' + Math.max(S.grid * 2, 8));
      const have = new Set(Q.items.map(x => x.uid));
      for (const m of r.emails) if (!have.has(m.uid) && !isActive(m.uid)) Q.items.push(m);
      Q.exhausted = r.emails.length === 0;
    } catch (e) {
      Q.exhausted = false;
    } finally {
      Q.fetching = false;
    }
  }

  // ------------------------------------------------------------ 舞台（网格 + canvas）
  const Stage = { el: null, cv: null, ctx: null, cells: [], w: 0, h: 0, dpr: 1 };
  function buildGrid(n) {
    S.grid = n;
    const [cols, rows] = GRIDS[n];
    // 还没分类完的邮件退回队列，别丢
    for (const c of Stage.cells) {
      c.dead = true;
      if (c.mail && c.state === 'crawl') Q.items.unshift(c.mail);   // 'jev' 状态的分类请求已发出，无需退回
    }
    for (const c of Stage.cells) c.root.remove();
    Stage.cells = [];
    Stage.el.style.gridTemplateColumns = 'repeat(' + cols + ',1fr)';
    Stage.el.style.gridTemplateRows = 'repeat(' + rows + ',1fr)';
    for (let i = 0; i < n; i++) {
      const c = new Cell(i, FS[n]);
      Stage.cells.push(c);
      Stage.el.append(c.root);
    }
    Stage.el.append(Stage.cv);
    for (const b of document.querySelectorAll('#grids button')) b.classList.toggle('on', +b.dataset.n === n);
    requestAnimationFrame(layoutAll);
  }
  function resizeCanvas() {
    const r = Stage.el.getBoundingClientRect();
    Stage.dpr = window.devicePixelRatio || 1;
    Stage.w = r.width; Stage.h = r.height;
    Stage.cv.width = Math.max(1, Math.round(r.width * Stage.dpr));
    Stage.cv.height = Math.max(1, Math.round(r.height * Stage.dpr));
    Stage.ctx.setTransform(Stage.dpr, 0, 0, Stage.dpr, 0, 0);
  }
  function layoutAll() {
    if (S.view !== 'crawl') return;
    resizeCanvas();
    for (const c of Stage.cells) c.layout();
  }

  let lastT = 0;
  function frame(t) {
    requestAnimationFrame(frame);
    if (S.view !== 'crawl') { lastT = t; return; }
    const dt = Math.min(0.05, (t - lastT) / 1000 || 0.016);
    lastT = t;
    const ctx = Stage.ctx;
    ctx.clearRect(0, 0, Stage.w, Stage.h);
    for (const c of Stage.cells) { c.update(dt); c.draw(ctx); }
  }

  // ------------------------------------------------------------ 顶部计数 / 侧栏计数 / 轮询
  function updateCounters() {
    const crawling = S.view === 'crawl' ? Stage.cells.filter(c => c.state === 'crawl' || c.state === 'jev').length : 0;
    odo.crawlers.set(crawling);
    const p = S.prog || {};
    odo.emails.set(p.emails_in_db || 0);
    $('#s-emails').textContent = p.total_found ? '/ ' + fmt(p.total_found) : '';
    odo.words.set(W.total);
    odo.jev.set(p.sent_to_jev || 0);       // 服务器记录的真实调用次数
    odo.exp.set(S.stats ? S.stats.expired : 0);
  }
  function renderSide() {
    const st = S.stats;
    const mk = (id, label, cnt, color) => {
      const d = el('div', 'nav' + (S.view === id ? ' on' : ''));
      d.dataset.view = id;
      const dot = el('i', 'dot');
      if (color) dot.style.background = color;
      d.append(dot, el('span', 'nm', label), el('span', 'ct', cnt == null ? '' : fmt(cnt)));
      return d;
    };
    const views = $('#nav-views');
    views.replaceChildren(
      mk('crawl', '爬取中', null, '#5eead4'),
      mk('all', '全部', st ? st.total : null, '#7aa2ff'),
      mk('todo', '待处理', st ? st.todo : null, '#f1c453'),
      mk('expired', '已过期 · 可删', st ? st.expired : null, '#b58a8e'));
    const cats = $('#nav-cats');
    const zh = (st && st.cat_zh) || CAT_ZH;
    cats.replaceChildren(...CAT_ORDER.map(k => mk('cat:' + k, zh[k] || k, st ? st.cats[k] : null, CAT_COLOR[k])));
    $('#cost').textContent = '$' + (st ? st.cost_usd : 0).toFixed(4);
  }
  function renderSync() {
    const p = S.prog || {};
    const line = $('#syncline'), pct = $('#syncpct'), bar = $('#syncbar');
    if (p.syncing) {
      line.textContent = p.phase === '连接邮箱' ? '连接邮箱…' : '同步中 ' + fmt(p.synced) + '/' + fmt(p.to_sync);
      const v = p.to_sync ? p.synced / p.to_sync : 0;
      pct.textContent = Math.round(v * 100) + '%';
      bar.style.width = (v * 100) + '%';
    } else if (p.error) {
      line.textContent = '同步出错';
      pct.textContent = '';
      bar.style.width = '0';
      line.title = p.error;
    } else {
      line.textContent = p.last_finished ? '已同步 ' + fmt(p.emails_in_db) + ' 封' : '未同步';
      pct.textContent = p.classified ? '已分类 ' + fmt(p.classified) : '';
      bar.style.width = p.emails_in_db ? (p.classified / p.emails_in_db * 100) + '%' : '0';
    }
  }
  async function fetchProgress() {
    try {
      const p = await api('/api/progress');
      S.prog = p;
      if (!W.inited) { W.total = (p.words_read || 0) + W.pending; W.inited = true; }
      renderSync();
    } catch (e) { /* 服务暂时不可达，下次再试 */ }
  }
  function pollLoop() {   // 只有一条轮询链
    fetchProgress().then(() => setTimeout(pollLoop, (S.prog && S.prog.syncing) || S.view === 'crawl' ? 900 : 3000));
  }
  async function refreshStats() {
    try {
      S.stats = await api('/api/stats');
      renderSide();
    } catch (e) { /* ignore */ }
  }
  let refreshT = 0;
  function scheduleRefresh() {
    clearTimeout(refreshT);
    refreshT = setTimeout(async () => {
      await refreshStats();
      try { S.prog = await api('/api/progress'); renderSync(); } catch (e) { /* ignore */ }
    }, 400);
  }
  async function flushWords() {
    if (W.pending > 0) {
      const n = W.pending;
      W.pending = 0;
      try { await post('/api/words', { n }); } catch (e) { W.pending += n; }
    }
  }

  // ------------------------------------------------------------ 视图切换
  function setView(v) {
    S.view = v;
    const crawl = v === 'crawl';
    document.body.classList.toggle('listmode', !crawl);
    $('#crawlview').hidden = !crawl;
    $('#listview').hidden = crawl;
    renderSide();
    if (crawl) {
      requestAnimationFrame(layoutAll);
      refill();
    } else {
      L.reset();
      loadList(true);
    }
  }

  // ------------------------------------------------------------ 列表视图
  const L = {
    items: [], total: 0, loading: false, done: false, sel: new Set(),
    reset() { this.items = []; this.total = 0; this.done = false; this.sel = new Set(); this.loading = false; $('#lbody').replaceChildren(); }
  };
  const VIEW_NAME = { all: '全部', todo: '待处理', expired: '已过期 · 可删' };
  function viewTitle() {
    if (S.view.startsWith('cat:')) return ((S.stats && S.stats.cat_zh) || CAT_ZH)[S.view.slice(4)] || S.view;
    return VIEW_NAME[S.view] || S.view;
  }
  function buildHead() {
    const exp = S.view === 'expired';
    const cols = [];
    if (exp) cols.push(['', 38]);
    cols.push(['日期', 118], ['类别', 150], ['主题', 0], ['发件人', 210], ['DDL', 130], ['状态', 84]);
    const tr = $('#lhead');
    tr.replaceChildren();
    for (const [name, w] of cols) {
      const th = el('th', '', name);
      if (w) th.style.width = w + 'px';
      tr.append(th);
    }
  }
  async function loadList(reset) {
    if (L.loading || (L.done && !reset)) return;
    L.loading = true;
    const myView = S.view, myQ = S.q;
    if (reset) buildHead();
    try {
      const r = await api('/api/emails?view=' + encodeURIComponent(S.view) + '&q=' + encodeURIComponent(S.q) +
        '&limit=200&offset=' + L.items.length);
      if (myView !== S.view || myQ !== S.q) return;   // 期间切了视图，丢弃
      L.total = r.total;
      const start = L.items.length;
      L.items.push(...r.items);
      appendRows(start);
      L.done = L.items.length >= r.total || r.items.length === 0;
    } catch (e) {
      toast('加载列表失败：' + e.message, true);
    } finally {
      L.loading = false;
      renderListHeader();
    }
  }
  function renderListHeader() {
    $('#ltitle').textContent = viewTitle() + (S.q ? '  · 搜索「' + S.q + '」' : '');
    $('#lcount').textContent = fmt(L.items.length) + ' / ' + fmt(L.total) + ' 封';
    const exp = S.view === 'expired';
    $('#lacts').hidden = !exp;
    const n = L.sel.size;
    const b = $('#btn-trash');
    b.disabled = n === 0;
    b.textContent = n ? '移到已删除（' + n + '）' : '移到已删除';
    const all = $('#selall');
    all.checked = L.items.length > 0 && n === L.items.length;
    all.indeterminate = n > 0 && n < L.items.length;
    $('#lmore').textContent = L.done ? (L.items.length ? '— 到底了 —' : '这里还没有邮件') : '向下滚动加载更多…';
  }
  function appendRows(start) {
    const exp = S.view === 'expired';
    const tb = $('#lbody');
    const frag = document.createDocumentFragment();
    for (let i = start; i < L.items.length; i++) {
      const m = L.items[i];
      const tr = el('tr', L.sel.has(m.uid) ? 'sel' : '');
      tr.dataset.uid = m.uid;
      if (exp) {
        const td = el('td');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = L.sel.has(m.uid);
        cb.addEventListener('click', ev => ev.stopPropagation());
        cb.addEventListener('change', () => {
          if (cb.checked) L.sel.add(m.uid); else L.sel.delete(m.uid);
          tr.classList.toggle('sel', cb.checked);
          renderListHeader();
        });
        td.append(cb);
        tr.append(td);
      }
      tr.append(el('td', 'mono', (m.date || '').slice(0, 16)));
      const tc = el('td');
      if (m.classified) {
        const pill = el('span', 'pill');
        const dot = el('i');
        dot.style.background = CAT_COLOR[m.cat] || '#888';
        pill.append(dot, el('span', '', m.cat_zh || m.cat));
        tc.append(pill);
      } else {
        tc.append(el('span', 'conf', '未分类'));
        tc.style.color = 'var(--dim2)';
      }
      tr.append(tc);
      const ts = el('td', m.seen ? '' : 'unread');
      if (m.classified && m.action >= 0.6) ts.append(el('span', 'hand', '✋'));
      ts.append(document.createTextNode(m.subject || '(无主题)'));
      ts.title = m.subject || '';
      tr.append(ts);
      const tf = el('td', '', shortFrom(m.sender));
      tf.title = m.sender || '';
      tr.append(tf);
      tr.append(el('td', 'mono', m.deadline ? fmtDdl(m.deadline) : ''));
      const tst = el('td');
      if (m.classified) tst.append(el('span', 'badge b-' + m.ddl_status, DDL_ZH[m.ddl_status] || ''));
      tr.append(tst);
      tr.addEventListener('click', () => openDrawer(m.uid));
      frag.append(tr);
    }
    tb.append(frag);
  }
  async function selectAll(on) {
    if (on) {
      while (!L.done && !L.loading) {   // 先把剩下的都加载出来
        const before = L.items.length;
        await loadList(false);
        if (L.items.length === before) break;
      }
      for (const m of L.items) L.sel.add(m.uid);
    } else {
      L.sel.clear();
    }
    for (const tr of $('#lbody').children) {
      const uid = +tr.dataset.uid, c = tr.querySelector('input');
      const on2 = L.sel.has(uid);
      tr.classList.toggle('sel', on2);
      if (c) c.checked = on2;
    }
    renderListHeader();
  }

  // ------------------------------------------------------------ 抽屉 / 弹窗
  async function openDrawer(uid) {
    const dr = $('#drawer'), box = $('#dcontent');
    box.replaceChildren(el('div', 'conf', '加载中…'));
    dr.classList.add('open');
    dr.setAttribute('aria-hidden', 'false');
    $('#drawer-mask').hidden = false;
    try {
      const m = await api('/api/email/' + uid);
      const meta = el('div', 'dmeta');
      const add = (k, v, node) => { meta.append(el('div', '', k)); const d = el('div'); if (node) d.append(node); else d.textContent = v; meta.append(d); };
      add('日期', m.date || '');
      add('发件人', m.sender || '');
      if (m.cat) {
        const pill = el('span', 'pill');
        const dot = el('i'); dot.style.background = CAT_COLOR[m.cat] || '#888';
        pill.append(dot, el('span', '', (m.cat_zh || m.cat) + ' · ' + Math.round((m.cat_conf || 0) * 100) + '%'));
        add('类别', '', pill);
        add('要动手', (m.action >= 0.6 ? '是 ' : '否 ') + Math.round((m.action || 0) * 100) + '%');
        add('DDL', '', (() => {
          const s = el('span');
          s.append(document.createTextNode((m.deadline ? fmtDdl(m.deadline) + (m.deadline_text ? '（原文：' + m.deadline_text + '）' : '') : '') + ' '),
            el('span', 'badge b-' + m.ddl_status, DDL_ZH[m.ddl_status] || ''));
          return s;
        })());
      }
      box.replaceChildren(el('div', 'dsubj', m.subject || '(无主题)'), meta, el('div', 'dbody', m.body || '（正文为空）'));
    } catch (e) {
      box.replaceChildren(el('div', 'conf', '加载失败：' + e.message));
    }
  }
  function closeDrawer() {
    $('#drawer').classList.remove('open');
    $('#drawer').setAttribute('aria-hidden', 'true');
    $('#drawer-mask').hidden = true;
  }
  function confirmDialog(text, okLabel) {
    return new Promise(res => {
      $('#modal-text').textContent = text;
      $('#m-ok').textContent = okLabel || '确认';
      $('#modal-mask').hidden = false;
      const done = v => {
        $('#modal-mask').hidden = true;
        $('#m-ok').onclick = $('#m-cancel').onclick = null;
        res(v);
      };
      $('#m-ok').onclick = () => done(true);
      $('#m-cancel').onclick = () => done(false);
    });
  }
  async function doTrash() {
    const uids = [...L.sel];
    if (!uids.length) return;
    const ok = await confirmDialog('将把 ' + uids.length + ' 封移到邮箱的『已删除』文件夹，可在网页邮箱里恢复。', '确认移动');
    if (!ok) return;
    const b = $('#btn-trash');
    b.disabled = true;
    b.textContent = '移动中…';
    try {
      const r = await post('/api/trash', { uids });
      const moved = new Set(r.moved || []);
      L.items = L.items.filter(m => !moved.has(m.uid));
      for (const u of moved) L.sel.delete(u);
      L.total = Math.max(0, L.total - moved.size);
      $('#lbody').replaceChildren();
      appendRows(0);
      toast('已移到『' + (r.folder || '已删除') + '』：' + moved.size + ' 封' + ((r.failed || []).length ? '，失败 ' + r.failed.length + ' 封' : ''), (r.failed || []).length > 0);
      refreshStats();
    } catch (e) {
      toast('移动失败：' + e.message, true);
    }
    renderListHeader();
  }

  // ------------------------------------------------------------ 登录 / 设置
  // 学号 → 邮箱的猜测规则（只用于自动填充，用户可以改）：去掉学号首位、前面加 v。
  // 该规则来自零星样本，未获学校确认，因此只做建议。
  function guessEmail(sid) {
    if (/^\d{10}$/.test(sid)) return 'v' + sid.slice(1) + '@mail.bnbu.edu.cn';
    return '';
  }

  async function boot() {
    let setup = null;
    try { setup = await api('/api/setup'); } catch (e) { /* 服务未就绪 */ }
    if (setup && setup.logged_in) {
      S.setup = setup;
      applySetup();
      $('#login').hidden = true;
      init();
    } else {
      showLogin();
    }
  }

  function showLogin() {
    $('#login').hidden = false;
    $('#sidebar').style.display = 'none';
    $('#main').style.display = 'none';
    const sidEl = $('#lg-sid'), emailEl = $('#lg-email');
    let lastGuess = '';
    sidEl.addEventListener('input', () => {
      const g = guessEmail(sidEl.value.trim());
      if (g) {
        if (!emailEl.value || emailEl.value === lastGuess) emailEl.value = g;
        lastGuess = g;
      }
    });
    $('#lg-submit').onclick = doLogin;
    for (const id of ['lg-sid', 'lg-email', 'lg-pass', 'lg-key']) {
      $('#' + id).addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
    }
  }

  async function doLogin() {
    const err = $('#lg-error');
    err.hidden = true;
    const body = {
      email: $('#lg-email').value.trim(),
      password: $('#lg-pass').value,
      jev_key: $('#lg-key').value.trim(),
      since: $('#lg-since').value, before: $('#lg-before').value,
      auto_sync: $('#lg-auto').checked
    };
    if (!body.email || !body.password || !body.jev_key) {
      err.textContent = '邮箱、密码和 API key 都要填。';
      err.hidden = false;
      return;
    }
    const b = $('#lg-submit');
    b.disabled = true; b.textContent = '登录中…';
    try {
      const setup = await post('/api/login', body);
      S.setup = setup;
      $('#login').hidden = true;
      $('#sidebar').style.display = '';
      $('#main').style.display = '';
      applySetup();
      init();
    } catch (e) {
      err.textContent = e.message || String(e);
      err.hidden = false;
    } finally {
      b.disabled = false; b.textContent = '登录并开始';
    }
  }

  function applySetup() {
    const p = (S.setup && S.setup.progress) || null;
    if (p) S.prog = p;
  }

  function openSettings() {
    const cfg = S.setup || {};
    $('#st-email').textContent = cfg.email || '';
    $('#st-since').value = cfg.since || '';
    $('#st-before').value = cfg.before || '';
    $('#st-auto').checked = !!cfg.auto_sync;
    $('#st-min').value = cfg.auto_sync_minutes || 30;
    $('#st-error').hidden = true;
    $('#settings-mask').hidden = false;
  }
  function closeSettings() { $('#settings-mask').hidden = true; }

  async function saveSettings() {
    const err = $('#st-error');
    err.hidden = true;
    const body = {
      since: $('#st-since').value, before: $('#st-before').value,
      auto_sync: $('#st-auto').checked, auto_sync_minutes: parseInt($('#st-min').value, 10) || 30
    };
    try {
      const cfg = await post('/api/settings', body);
      S.setup = Object.assign(S.setup || {}, cfg, { logged_in: true });
      closeSettings();
      toast('设置已保存；同步范围对下一轮爬取生效');
    } catch (e) {
      err.textContent = e.message || String(e);
      err.hidden = false;
    }
  }

  async function doLogout() {
    const ok = await confirmDialog('退出登录会清除本机保存的密码、API key 和邮件数据库（邮箱服务器上的邮件不受影响）。确定退出？', '退出登录');
    if (!ok) return;
    try { await post('/api/logout'); } catch (e) { /* 照样刷新 */ }
    location.reload();
  }

  // ------------------------------------------------------------ 事件与启动
  async function newRound() {
    try {
      const r = await post('/api/sync');
      toast(r.started ? '开始同步邮箱（只读），新邮件会自动被爬虫取走' : '同步已经在进行中');
    } catch (e) { toast('启动同步失败：' + e.message, true); }
    Q.exhausted = false; Q.last = 0;
    for (const c of Stage.cells) if (c.state === 'idle') c.setMsg('取信中…', false);
    if (S.view !== 'crawl') setView('crawl');
    fetchProgress();
  }
  function bind() {
    $('#btn-crawl').onclick = newRound;
    $('#btn-fold').onclick = () => { document.body.classList.toggle('collapsed'); setTimeout(layoutAll, 260); };
    $('#btn-search').onclick = () => {
      const b = $('#searchbox');
      b.hidden = !b.hidden;
      if (!b.hidden) $('#q').focus();
    };
    let qt = 0;
    $('#q').addEventListener('input', e => {
      clearTimeout(qt);
      qt = setTimeout(() => {
        S.q = e.target.value.trim();
        if (S.view === 'crawl') { if (!S.q) return; setView('all'); } else { L.reset(); loadList(true); }
      }, 300);
    });
    $('#sidebar').addEventListener('click', e => {
      const n = e.target.closest('.nav');
      if (n) setView(n.dataset.view);
    });
    $('#grids').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (b) buildGrid(+b.dataset.n);
    });
    $('#speed').addEventListener('input', e => {
      S.speed = +e.target.value;
      $('#speedv').textContent = S.speed.toFixed(1) + '×';
    });
    $('#lwrap').addEventListener('scroll', e => {
      const w = e.target;
      if (w.scrollTop + w.clientHeight > w.scrollHeight - 300) loadList(false);
    });
    $('#selall').addEventListener('change', e => selectAll(e.target.checked));
    $('#btn-trash').onclick = doTrash;
    $('#btn-settings').onclick = openSettings;
    $('#st-close').onclick = closeSettings;
    $('#st-save').onclick = saveSettings;
    $('#st-logout').onclick = doLogout;
    $('#settings-mask').addEventListener('click', e => { if (e.target.id === 'settings-mask') closeSettings(); });
    $('#drawer-close').onclick = closeDrawer;
    $('#drawer-mask').onclick = closeDrawer;
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') { closeDrawer(); if (!$('#modal-mask').hidden) $('#m-cancel').click(); }
    });
    let rt = 0;
    window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(layoutAll, 120); });
  }

  function init() {
    Stage.el = $('#stage');
    Stage.cv = $('#cv');
    Stage.ctx = Stage.cv.getContext('2d');
    odo.crawlers = new Odo($('#o-crawlers'));
    odo.emails = new Odo($('#o-emails'));
    odo.words = new Odo($('#o-words'));
    odo.jev = new Odo($('#o-jev'));
    odo.exp = new Odo($('#o-exp'));
    for (const k in odo) odo[k].set(0);
    bind();
    renderSide();
    buildGrid(9);
    setView('crawl');
    refreshStats();
    pollLoop();
    setInterval(updateCounters, 250);
    setInterval(() => { if (S.view === 'crawl' && Q.items.length < S.grid) refill(); }, 500);
    setInterval(flushWords, 2000);
    setInterval(refreshStats, 4000);
    requestAnimationFrame(frame);
  }
  if (typeof document !== 'undefined' && document.getElementById('stage')) boot();
})();
