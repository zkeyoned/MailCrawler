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

  const CAT_ORDER = ['coursework', 'academic_admin', 'campus_life', 'event', 'career', 'newsletter', 'personal', 'automated', 'ignored'];
  const CAT_COLOR = {
    coursework: '#ff6b6b', academic_admin: '#ffa94d', campus_life: '#69db7c', event: '#4dabf7',
    career: '#da77f2', newsletter: '#7d8799', personal: '#ffd43b', automated: '#5c7f9e', ignored: '#4a5463'
  };
  const CAT_ZH = {
    coursework: '课程作业考试', academic_admin: '教务行政', campus_life: '校园生活与服务',
    event: '讲座活动比赛', career: '实习招聘', newsletter: '群发通知与订阅', personal: '写给你本人',
    automated: '系统自动通知', ignored: '已忽略（发件人规则）'
  };
  const STAR_ZH = { 5: '必须马上处理', 4: '这周要处理', 3: '值得看一眼', 2: '可看可不看', 1: '完全不用看' };
  // 星级：★ 实心 + ☆ 空心；没有分数（老邮件还没重新判断）返回 null
  function starsEl(n, imp) {
    if (!n) return null;
    const s = el('span', 'stars s' + n, '★'.repeat(n) + '☆'.repeat(5 - n));
    s.title = STAR_ZH[n] + (imp != null ? '（' + Number(imp).toFixed(1) + '）' : '');
    return s;
  }
  const DDL_ZH = { expired: '已过期', open: '还没过', none: '无截止' };
  const EV_ZH = { upcoming: '还没开始', ongoing: '进行中', ended: '已结束' };

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
  const S = { view: 'crawl', grid: 9, speed: 1, q: '', stats: null, prog: null, setup: null, sort: 'date', minStars: '', maxStars: '' };
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
  function fmtDdl(dl, full) {   // '2026-10-16 17:00' -> '10月16日 17:00'；23:59（邮件里只写了日期）默认不显示，full=true 时照写
    const m = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?/.exec(dl || '');
    if (!m) return dl || '';
    const t = (m[4] && (full || !(m[4] === '23' && m[5] === '59'))) ? ' ' + m[4] + ':' + m[5] : '';
    return (+m[2]) + '月' + (+m[3]) + '日' + t;
  }

  // 活动时间：'2026-10-19 14:00' + '2026-10-19 15:30' -> '10月19日（周一）14:00–15:30'；全天（00:00–23:59）写「全天」，没有结束时间只写开始
  function isAllDay(st, en) { return (st || '').slice(11, 16) === '00:00' && (en || '').slice(11, 16) === '23:59'; }
  function fmtEvent(st, en, withDate) {
    const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(st || '');
    if (!m) return st || '';
    const wd = WEEK_ZH[new Date(+m[1], +m[2] - 1, +m[3]).getDay()];
    const date = (+m[2]) + '月' + (+m[3]) + '日（周' + wd + '）';
    let t;
    if (isAllDay(st, en)) t = '全天';
    else {
      t = m[4] + ':' + m[5];
      if (en && en > st && en.slice(0, 10) === st.slice(0, 10)) t += '–' + en.slice(11, 16);
    }
    return (withDate === false ? '' : date) + t;
  }
  function evBadge(m) { return m.ev_status && m.ev_status !== 'none' ? el('span', 'badge b-' + m.ev_status, EV_ZH[m.ev_status] || '') : null; }
  function ddlBadge(m) { return m.ddl_status && m.ddl_status !== 'none' ? el('span', 'badge b-' + m.ddl_status, DDL_ZH[m.ddl_status] || '') : null; }

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
      const st = starsEl(r.stars, r.importance);
      if (st) row1.append(st);
      const timeRows = [];
      if (r.event_start) {
        const rw = el('div', 'vrow');
        rw.append(el('span', '', '📅'), el('span', '', fmtEvent(r.event_start, r.event_end) + (r.event_place ? ' · ' + r.event_place : '')), evBadge(r));
        timeRows.push(rw);
      }
      if (r.deadline) {
        const rw = el('div', 'vrow');
        rw.append(el('span', '', '⏳'), el('span', '', '截止 ' + fmtDdl(r.deadline)), ddlBadge(r));
        timeRows.push(rw);
      }
      if (!timeRows.length) {
        const rw = el('div', 'vrow');
        rw.append(el('span', '', '📅'), el('span', 'badge b-none', '无截止'));
        timeRows.push(rw);
      }
      const row3 = el('div', 'vrow');
      const act = (r.action || 0) >= 0.6;
      row3.append(el('span', 'badge ' + (act ? 'b-todo' : 'b-none'), act ? '✋ 要动手' : '不用动手'),
        el('span', 'conf', Math.round((r.action || 0) * 100) + '%'));
      this.verdict.replaceChildren(row1, ...timeRows, row3);
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
      mk('calendar', '📅 日历', null, '#c4a6ff'),
      mk('all', '全部', st ? st.total : null, '#7aa2ff'),
      mk('todo', '待处理', st ? st.todo : null, '#f1c453'),
      mk('expired', '已过期 · 可删', st ? st.expired : null, '#b58a8e'));
    const cats = $('#nav-cats');
    const zh = (st && st.cat_zh) || CAT_ZH;
    cats.replaceChildren(...CAT_ORDER.filter(k => k !== 'ignored' || (st && st.cats[k])).map(k => mk('cat:' + k, zh[k] || k, st ? st.cats[k] : null, CAT_COLOR[k])));
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
    const cal = v === 'calendar';
    $('#crawlview').hidden = !crawl;
    $('#calview').hidden = !cal;
    $('#listview').hidden = crawl || cal;
    renderSide();
    if (!cal) Cal.leave();
    if (cal) {
      Cal.enter();
    } else if (crawl) {
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
    cols.push(['日期', 118], ['类别', 150], ['重要', 84], ['主题', 0], ['发件人', 170], ['时间', 250], ['状态', 92]);
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
        '&sort=' + S.sort + '&min=' + S.minStars + '&max=' + S.maxStars + '&limit=200&offset=' + L.items.length);
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
      const tstar = el('td');
      const se = starsEl(m.stars, m.importance);
      if (se) tstar.append(se);
      tr.append(tstar);
      const ts = el('td', m.seen ? '' : 'unread');
      if (m.classified && m.action >= 0.6) ts.append(el('span', 'hand', '✋'));
      ts.append(document.createTextNode(m.subject || '(无主题)'));
      ts.title = m.subject || '';
      tr.append(ts);
      const tf = el('td', '', shortFrom(m.sender));
      tf.title = m.sender || '';
      tr.append(tf);
      // 时间列：优先活动时间区间，其次截止；状态标签同理
      const tw = el('td', 'mono', m.event_start ? fmtEvent(m.event_start, m.event_end) : (m.deadline ? '截止 ' + fmtDdl(m.deadline) : ''));
      if (m.event_start) tw.title = '活动' + (m.event_place ? ' · ' + m.event_place : '') + (m.deadline ? '\n截止 ' + fmtDdl(m.deadline) : '');
      tr.append(tw);
      const tst = el('td');
      if (m.classified) { const bg = m.event_start ? evBadge(m) : ddlBadge(m); if (bg) tst.append(bg); }
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

  // ------------------------------------------------------------ 日历视图
  // 邮件文字一律 textContent；日期全部用本地时间的 YYYY-MM-DD 字符串比较，不经过 UTC。
  const WEEK_ZH = ['日', '一', '二', '三', '四', '五', '六'];
  const pad2 = n => (n < 10 ? '0' : '') + n;
  const ymd = d => d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  const parseYmd = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s); return new Date(+m[1], +m[2] - 1, +m[3]); };
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 存不了就算了 */ } }

  // ------------------------------------------------------------ 小图标（常量 SVG，不含邮件内容）
  const SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
  const ICONS = {
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    pin: '<path d="M12 21s-7-6.2-7-11a7 7 0 0114 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
    warn: '<path d="M12 3.5l10 17.5H2z"/><path d="M12 10v5M12 18h.01"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    dl: '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
  };
  const icoTpl = document.createElement('template');
  function ico(name) {
    icoTpl.innerHTML = SVG_OPEN + ICONS[name] + '</svg>';
    return icoTpl.content.firstChild.cloneNode(true);
  }
  const mondayOf = d => new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
  const hmMin = s => { const m = /^(\d{1,2}):(\d{2})/.exec(s || ''); return m ? +m[1] * 60 + +m[2] : 0; };
  const DOW_EN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const HP = 56, PAD = 8;     // 一小时的像素高度；网格上下留白

  // 把重叠的块排成并排的几列：返回时每块带 lane / lanes
  function layoutLanes(blks) {
    blks.sort((a, b) => a.s - b.s || b.e - a.e);
    let cluster = [], laneEnds = [], clusterEnd = -1;
    const flush = () => { for (const b of cluster) b.lanes = laneEnds.length; };
    for (const b of blks) {
      if (cluster.length && b.s >= clusterEnd) { flush(); cluster = []; laneEnds = []; clusterEnd = -1; }
      let l = laneEnds.findIndex(e => e <= b.s);
      if (l < 0) { l = laneEnds.length; laneEnds.push(0); }
      laneEnds[l] = b.e; b.lane = l;
      cluster.push(b); clusterEnd = Math.max(clusterEnd, b.e);
    }
    flush();
    return blks;
  }

  const Cal = {
    mode: lsGet('mc_cal_mode') === 'month' ? 'month' : 'week',
    weekStart: null,      // 当前周的周一（Date）
    month: null,          // 当月 1 号（Date）
    sel: '',              // 月视图选中的日期 YYYY-MM-DD
    items: [],            // 月历范围内全部条目（未筛选）
    upcoming: [],         // 今天起 14 天（未筛选）
    wk: null,             // /api/week 的结果
    min3: lsGet('mc_cal_min3') !== '0',   // 默认只看 ≥3 星
    hideExp: lsGet('mc_cal_hideexp') === '1',
    gridFrom: '', gridTo: '',
    seq: 0,
    timer: 0,
    scrolledFor: '',
    ttWarned: false,
    autoFolded: false,    // 周视图为了让每列够宽，自动折叠了左侧栏（离开日历时还原）

    // 保证周视图每列 ≥120px：先试议程栏 320px，不够缩到 280px，还不够就自动折叠左侧栏
    fit() {
      const cv = $('#calview');
      const userFolded = document.body.classList.contains('collapsed') && !this.autoFolded;
      const sbw = userFolded ? 0 : 240;
      const colW = agw => (window.innerWidth - sbw - 36 - 18 - agw - 2 - 46) / 7;
      let agw = 320, fold = false;
      if (this.mode === 'week') {
        if (colW(320) >= 120) agw = 320;
        else if (colW(280) >= 120) agw = 280;
        else if (sbw > 0) { fold = true; agw = 320; }
        else agw = 280;
      }
      if (fold && !document.body.classList.contains('collapsed')) { document.body.classList.add('collapsed'); this.autoFolded = true; }
      else if (!fold && this.autoFolded) { document.body.classList.remove('collapsed'); this.autoFolded = false; }
      cv.style.setProperty('--agw', agw + 'px');
    },
    leave() {
      if (this.autoFolded) { document.body.classList.remove('collapsed'); this.autoFolded = false; setTimeout(layoutAll, 260); }
      clearInterval(this.timer);
    },

    enter() {
      const t = new Date();
      if (!this.month) this.month = new Date(t.getFullYear(), t.getMonth(), 1);
      if (!this.weekStart) this.weekStart = mondayOf(t);
      $('#cal-min3').checked = this.min3;
      $('#cal-hideexp').checked = this.hideExp;
      $('#cal-export').replaceChildren(ico('dl'));
      this.applyMode();
      this.fit();
      this.load();
      clearInterval(this.timer);
      this.timer = setInterval(() => { if (S.view === 'calendar') this.load(); else clearInterval(this.timer); }, 20000);
    },
    applyMode() {
      const wk = this.mode === 'week';
      $('#calweek').hidden = !wk;
      $('#calmonth').hidden = wk;
      for (const b of $('#cal-mode').children) b.classList.toggle('on', b.dataset.mode === this.mode);
      $('#cal-prev').title = wk ? '上一周' : '上个月';
      $('#cal-next').title = wk ? '下一周' : '下个月';
      this.updateTitle();
    },
    updateTitle() {
      if (this.mode === 'week') {
        const a = this.weekStart, b = addDays(a, 6);
        $('#cal-title').textContent = (a.getMonth() + 1) + '/' + a.getDate() + ' - ' + (b.getMonth() + 1) + '/' + b.getDate();
      } else {
        $('#cal-title').textContent = this.month.getFullYear() + '年' + (this.month.getMonth() + 1) + '月';
      }
    },
    setMode(m) {
      if (m === this.mode) return;
      if (m === 'month') {
        const d = this.sel ? parseYmd(this.sel) : this.weekStart;
        this.month = new Date(d.getFullYear(), d.getMonth(), 1);
      } else {
        const t = new Date();
        const cur = this.month.getFullYear() === t.getFullYear() && this.month.getMonth() === t.getMonth();
        this.weekStart = mondayOf(this.sel ? parseYmd(this.sel) : (cur ? t : this.month));
      }
      this.mode = m;
      lsSet('mc_cal_mode', m);
      this.applyMode();
      this.fit();
      this.load();
    },
    load() { return this.mode === 'week' ? this.loadWeek() : this.loadMonth(); },
    render() { if (this.mode === 'week') this.renderWeek(); else this.renderMonth(); },
    visible(m) {
      if (this.hideExp && m.past) return false;
      if (this.min3 && !(m.stars >= 3)) return false;
      return true;
    },
    // 活动的时间段文字：14:00–15:30（没写结束时间只写开始；全天活动写「全天」）
    evRange(m) { return m.all_day ? '全天' : (m.end_default ? m.start.slice(11, 16) : m.start.slice(11, 16) + '–' + m.end_time); },
    timeText(m) {
      if (m.kind === 'event') return this.evRange(m);
      return m.has_time ? '截止 ' + m.start.slice(11, 16) : '当天截止';
    },
    // 条目悬停/标题里的一行说明
    descLine(m) {
      if (m.kind === 'event') return '活动 ' + fmtEvent(m.start, m.end || (m.end_default ? '' : m.start.slice(0, 11) + m.end_time)) + (m.event_place ? ' · ' + m.event_place : '') + ' · ' + (EV_ZH[m.ev_status] || '');
      return '截止 ' + fmtDdl(m.start, true) + (m.past ? ' · 已过期' : '');
    },
    dots(n) {
      if (!n) return null;
      const d = el('span', 'dots s' + n, '●'.repeat(n));
      d.title = n + ' 星 · ' + STAR_ZH[n];
      return d;
    },
    courseColor(i) { return 'var(--c' + (i % 8) + ')'; },
    // 冲突提示文字：与《课程名》冲突 N 分钟
    conflictText(m) { return (m.conflicts || []).map(c => '与《' + c.name + '》冲突 ' + c.minutes + ' 分钟').join('；'); },

    // ================= 周视图 =================
    async loadWeek() {
      const my = ++this.seq;
      this.updateTitle();
      try {
        const w = await api('/api/week?start=' + ymd(this.weekStart));
        if (my !== this.seq) return;
        this.wk = w;
        if (w.timetable && w.timetable.error && !this.ttWarned) { this.ttWarned = true; toast(w.timetable.error, true); }
      } catch (e) {
        if (my === this.seq) toast('加载日历失败：' + e.message, true);
        return;
      }
      this.renderWeek();
    },
    // 悬停小提示（周视图邮件竖条用）
    tip(node, lines) {
      let t = $('#wktip');
      if (!t) { t = el('div'); t.id = 'wktip'; t.hidden = true; document.body.append(t); }
      node.addEventListener('mouseenter', () => {
        t.replaceChildren(...lines.map((s, i) => el('div', i === 0 ? 'tt' : i === 1 ? 'tm' : 'cf', s)));
        t.hidden = false;
        const r = node.getBoundingClientRect(), tr = t.getBoundingClientRect();
        t.style.left = (r.left - tr.width - 8 >= 8 ? r.left - tr.width - 8 : r.right + 8) + 'px';
        t.style.top = Math.max(8, Math.min(window.innerHeight - tr.height - 8, r.top)) + 'px';
      });
      node.addEventListener('mouseleave', () => { t.hidden = true; });
    },
    // 截止栏：每天最多 1 个标签（星级最高，同星取时间最早），其余合并成 +N，点 +N 议程滚到那天
    dueCell(list, k, cls) {
      const sorted = list.slice().sort((a, b) => (b.stars || 0) - (a.stars || 0) || a.start.localeCompare(b.start));
      const m = sorted[0];
      const c = el('div', (cls || 'duechip') + (m.past ? ' exp' : ''), m.subject || '(无主题)');
      c.title = this.descLine(m) + (m.stars ? ' · ★' + m.stars : '') + '\n' + (m.subject || '');
      c.addEventListener('click', () => openDrawer(m.uid));
      const nodes = [c];
      if (sorted.length > 1) {
        const more = el('button', 'duemore', '+' + (sorted.length - 1));
        more.title = '这天还有 ' + (sorted.length - 1) + ' 项，点击在右侧议程里查看';
        more.addEventListener('click', () => {
          const g = document.querySelector('#wkagenda .agday[data-day="' + k + '"]');
          if (g) { g.scrollIntoView({ behavior: 'smooth', block: 'start' }); g.classList.add('flash'); setTimeout(() => g.classList.remove('flash'), 1200); }
          else toast('这一天已经过去，议程里不再列出', false);
        });
        nodes.push(more);
      }
      return nodes;
    },
    courseBlock(o, h, past) {
      const b = el('div', 'wkblk course' + (past ? ' past' : '') + (h < 46 ? ' short' : ''));
      b.style.setProperty('--cc', this.courseColor(o.course_index));
      b.append(el('div', 'bt', o.name));
      if (h >= 40) {
        const bm = el('div', 'bm');
        const tm = o.start + '–' + o.end;
        if (h < 84 && o.room) bm.append(el('span', '', tm + ' · ' + o.room));
        else { bm.append(el('span', '', tm)); if (o.room) bm.append(el('span', '', o.room)); }
        b.append(bm);
      }
      b.title = o.name + (o.code ? '（' + o.code + '）' : '') + '\n' + o.start + '–' + o.end + (o.room ? '  ' + o.room : '') + (o.teacher ? '\n' + o.teacher : '');
      return b;
    },
    // 邮件活动：不画成块，只在该时段、该天列的右边缘画一条 4px 竖条（类别色；与课冲突=红色）
    mailStrip(m, hpx) {
      const conf = (m.conflicts || []).length > 0;
      const b = el('div', 'wkstrip' + (m.past ? ' exp' : '') + (conf ? ' hasconf' : '') + (m.ev_status === 'ongoing' ? ' live' : ''));
      b.style.setProperty('--cc', conf ? 'var(--due)' : (CAT_COLOR[m.cat] || '#8794a8'));
      b.style.height = hpx + 'px';
      const lines = [m.subject || '(无主题)', this.evRange(m) + (m.event_place ? ' · ' + m.event_place : '') + (m.stars ? ' · ★' + m.stars : '') + (m.ev_status === 'ongoing' ? ' · 进行中' : m.past ? ' · 已结束' : '')];
      if (conf) lines.push(this.conflictText(m));
      this.tip(b, lines);
      b.addEventListener('click', () => openDrawer(m.uid));
      return b;
    },
    renderWeek() {
      const w = this.wk;
      if (!w) return;
      this.updateTitle();
      const ws = this.weekStart;
      const days = [0, 1, 2, 3, 4, 5, 6].map(i => addDays(ws, i));
      const keys = days.map(ymd);
      const now = new Date(), today = ymd(now), nowMin = now.getHours() * 60 + now.getMinutes();
      const tt = w.timetable || {};
      const hol = {};
      for (const h of tt.holidays || []) hol[h.date] = h.label;
      const occBy = {}, evBy = {}, dueBy = {}, adBy = {};
      for (const o of tt.occurrences || []) (occBy[o.date] = occBy[o.date] || []).push(o);
      const vis = (w.items || []).filter(m => this.visible(m));
      for (const m of vis) {
        const k = m.start.slice(0, 10);
        const grp = m.kind === 'due' ? dueBy : (m.all_day ? adBy : evBy);     // 截止栏只放截止；全天活动单独一行
        (grp[k] = grp[k] || []).push(m);
      }
      // 网格起点：默认 8 点，有更早的课/活动就往前扩
      let gs = 8;
      for (const o of tt.occurrences || []) gs = Math.min(gs, Math.floor(hmMin(o.start) / 60));
      for (const m of vis) if (m.kind === 'event' && !m.all_day) gs = Math.min(gs, Math.floor(hmMin(m.start.slice(11, 16)) / 60));
      const gridH = (24 - gs) * HP;
      $('#calview').style.setProperty('--hp', HP + 'px');
      const topOf = min => PAD + (min - gs * 60) / 60 * HP;

      // ---- 列头
      const head = [el('div', 'wkh')];
      keys.forEach((k, i) => {
        const h = el('div', 'wkh' + (k === today ? ' today' : '') + (hol[k] ? ' hol' : ''));
        h.append(el('b', '', DOW_EN[days[i].getDay()]), el('span', '', (days[i].getMonth() + 1) + '/' + days[i].getDate()));
        head.push(h);
      });
      $('#wkhead').replaceChildren(...head);

      // ---- 全天活动栏（这周没有全天活动就整栏隐藏）
      const adRow = $('#wkallday');
      const anyAd = keys.some(k => (adBy[k] || []).length);
      adRow.hidden = !anyAd;
      if (anyAd) {
        const cells = [el('div', '', '全天')];
        keys.forEach(k => {
          const c = el('div', (k === today ? 'today' : '') + (hol[k] ? ' hol' : ''));
          if ((adBy[k] || []).length) c.append(...this.dueCell(adBy[k], k, 'adchip'));
          cells.push(c);
        });
        adRow.replaceChildren(...cells);
      }

      // ---- 截止栏（固定一行；这周没有截止就整栏隐藏）
      const dueRow = $('#wkdue');
      const anyDue = keys.some(k => (dueBy[k] || []).length);
      dueRow.hidden = !anyDue;
      if (anyDue) {
        const cells = [el('div', '', '截止')];
        keys.forEach(k => {
          const c = el('div', (k === today ? 'today' : '') + (hol[k] ? ' hol' : ''));
          if ((dueBy[k] || []).length) c.append(...this.dueCell(dueBy[k], k));
          cells.push(c);
        });
        dueRow.replaceChildren(...cells);
      }

      // ---- 时间网格：只画课程（占满整列）；邮件活动是右边缘的竖条
      const gut = el('div', 'wkgut');
      gut.style.height = (gridH + PAD * 2) + 'px';
      for (let h = gs; h <= 24; h++) {
        const i = el('i', '', pad2(h) + ':00');
        i.style.top = (PAD + (h - gs) * HP) + 'px';
        gut.append(i);
      }
      const cols = [gut];
      keys.forEach(k => {
        const col = el('div', 'wkcol' + (k === today ? ' today' : '') + (hol[k] ? ' hol' : '') + (k < today ? ' past' : ''));
        col.style.height = (gridH + PAD * 2) + 'px';
        col.style.backgroundPosition = '0 ' + PAD + 'px';
        if (hol[k]) {
          const hd = el('div', 'wkhol');
          hd.append(ico('sun'), el('span', '', hol[k] + (/假/.test(hol[k]) ? '' : ' 假期')));
          col.append(hd);
        }
        // 竖条的并排道数决定课程块右边留多宽（一般 1 道：留 9px）
        const strips = layoutLanes((evBy[k] || []).map(m => ({ s: hmMin(m.start.slice(11, 16)), e: hmMin(m.end_time), mail: m })));
        const lanes = strips.reduce((n, b) => Math.max(n, b.lanes), 1);
        const reserve = 3 + lanes * 6;
        for (const o of occBy[k] || []) {
          const s = hmMin(o.start), e = hmMin(o.end);
          const hpx = Math.max((e - s) / 60 * HP - 2, 20);
          const node = this.courseBlock(o, hpx, k < today || (k === today && e <= nowMin));
          node.style.top = topOf(s) + 'px';
          node.style.height = hpx + 'px';
          node.style.left = '3px';
          node.style.right = reserve + 'px';
          col.append(node);
        }
        for (const b of strips) {
          const hpx = Math.max((b.e - b.s) / 60 * HP - 2, 16);
          const node = this.mailStrip(b.mail, hpx);
          node.style.top = topOf(b.s) + 'px';
          node.style.right = (2 + b.lane * 6) + 'px';
          col.append(node);
        }
        if (k === today && nowMin >= gs * 60) {
          const nl = el('div', 'wknow');
          nl.style.top = topOf(nowMin) + 'px';
          col.append(nl);
        }
        cols.push(col);
      });
      $('#wkgrid').replaceChildren(...cols);

      // 换周/首次进入时滚到合适位置（20 秒一次的自动刷新不动滚动条）
      const wkKey = ymd(ws) + '|' + gs;
      if (this.scrolledFor !== wkKey) {
        this.scrolledFor = wkKey;
        const cur = keys.includes(today);
        $('#wkscroll').scrollTop = cur && nowMin > 9 * 60 ? Math.max(0, topOf(nowMin - 90)) : 0;
      }
      this.renderAgenda(days, keys, hol, occBy, evBy, dueBy, adBy, today, nowMin);
    },
    card(kind, past) {
      const c = el('div', 'agcard ' + kind + (kind !== 'course' && kind !== 'holc' ? ' click' : '') + (past ? ' past' : ''));
      c.append(el('div', 'agbar'));
      const body = el('div', 'agbody');
      c.append(body);
      return [c, body];
    },
    // 第三行：左边地点/类别（省略号），右下角文字星级 ★4
    agRow3(left, stars) {
      const r3 = el('div', 'agr3');
      r3.append(el('span', 'rm', left || ''));
      if (stars) r3.append(el('span', 'agstar', '★' + stars));
      return r3;
    },
    renderAgenda(days, keys, hol, occBy, evBy, dueBy, adBy, today, nowMin) {
      const box = $('#wkagenda');
      box.replaceChildren();
      const curWeek = keys.includes(today);
      $('#wk-sidetitle').textContent = curWeek ? '本周议程' : $('#cal-title').textContent + ' 议程';
      let count = 0;
      keys.forEach((k, i) => {
        if (curWeek && k < today) return;
        const entries = [];
        for (const o of occBy[k] || []) entries.push({ t: hmMin(o.start), course: o });
        for (const m of adBy[k] || []) entries.push({ t: -1, mail: m });
        for (const m of evBy[k] || []) entries.push({ t: hmMin(m.start.slice(11, 16)), mail: m });
        for (const m of dueBy[k] || []) entries.push({ t: 24 * 60, due: m });
        if (!entries.length && !hol[k]) return;
        entries.sort((a, b) => a.t - b.t || (b.due ? (a.due ? (b.due.stars || 0) - (a.due.stars || 0) : -1) : 0));
        const g = el('div', 'agday');
        g.dataset.day = k;
        const d = days[i];
        const isToday = k === today;
        const h = el('div', 'agh' + (isToday ? ' today' : ''), '周' + WEEK_ZH[d.getDay()] + ' ' + (d.getMonth() + 1) + '/' + d.getDate());
        if (isToday) h.append(el('span', 'tag', '今天'));
        g.append(h);
        if (hol[k]) {
          const [c, b] = this.card('holc', false);
          b.append(el('div', 'agr2', hol[k] + (/假/.test(hol[k]) ? '' : ' 假期')));
          g.append(c);
        }
        for (const e of entries) {
          if (e.course) {
            const o = e.course;
            const [c, b] = this.card('course', k < today || (isToday && hmMin(o.end) <= nowMin));
            c.style.setProperty('--cc', this.courseColor(o.course_index));
            b.append(el('div', 'agr1', o.start + ' - ' + o.end), el('div', 'agr2', o.name));
            if (o.room) b.append(this.agRow3(o.room, 0));
            g.append(c);
          } else if (e.mail) {
            const m = e.mail;
            const [c, b] = this.card('mail', m.past);
            c.style.setProperty('--cc', CAT_COLOR[m.cat] || '#8794a8');
            const r1 = el('div', 'agr1');
            r1.append(ico('mail'), document.createTextNode(this.evRange(m)));
            if (m.ev_status === 'ongoing') r1.append(el('span', 'badge b-ongoing', '进行中'));
            b.append(r1, el('div', 'agr2', m.subject || '(无主题)'), this.agRow3(m.event_place || m.cat_zh || m.cat, m.stars));
            if ((m.conflicts || []).length) b.append(el('div', 'agwarn', this.conflictText(m)));
            c.addEventListener('click', () => openDrawer(m.uid));
            g.append(c);
          } else {
            const m = e.due;
            const [c, b] = this.card('due', m.past);
            b.append(el('div', 'agr1', m.has_time ? '截止 ' + m.start.slice(11, 16) : '当天截止'), el('div', 'agr2', m.subject || '(无主题)'), this.agRow3(m.cat_zh || m.cat, m.stars));
            c.addEventListener('click', () => openDrawer(m.uid));
            g.append(c);
          }
          count++;
        }
        box.append(g);
      });
      $('#wk-count').textContent = count + ' 项';
      if (!box.children.length) box.append(el('div', 'wkempty', (curWeek ? '这周剩下的日子' : '这一周') + '没有课程、活动或截止' + ((this.min3 || this.hideExp) ? '（当前有筛选）' : '')));
    },

    // ================= 月视图（原有） =================
    range() {
      const first = this.month;
      const off = (first.getDay() + 6) % 7;                       // 周一开头
      const start = new Date(first.getFullYear(), first.getMonth(), 1 - off);
      const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 41);   // 固定 6 周
      return [start, end];
    },
    async loadMonth() {
      const my = ++this.seq;
      this.updateTitle();
      const [a, b] = this.range();
      this.gridFrom = ymd(a); this.gridTo = ymd(b);
      const t = new Date();
      const up0 = ymd(t), up1 = ymd(new Date(t.getFullYear(), t.getMonth(), t.getDate() + 13));
      try {
        const [g, u] = await Promise.all([
          api('/api/calendar?from=' + this.gridFrom + '&to=' + this.gridTo),
          api('/api/calendar?from=' + up0 + '&to=' + up1)]);
        if (my !== this.seq) return;
        this.items = g.items;
        this.upcoming = u.items;
      } catch (e) {
        if (my === this.seq) toast('加载日历失败：' + e.message, true);
        return;
      }
      this.renderMonth();
    },
    chip(m) {
      const c = el('div', 'chip s' + (m.stars || 0) + (m.past ? ' exp' : '') + (m.kind === 'due' ? ' dueb' : ''));
      c.style.setProperty('--cc', CAT_COLOR[m.cat] || '#888');
      if (m.has_time) c.append(el('span', 'ct2', m.start.slice(11, 16)));
      c.append(el('span', 'cs', m.subject || '(无主题)'));
      const d = this.dots(m.stars);
      if (d) c.append(d);
      c.title = (m.subject || '') + '\n' + this.descLine(m) + ' · ' + (m.cat_zh || '');
      c.addEventListener('click', ev => { ev.stopPropagation(); openDrawer(m.uid); });
      return c;
    },
    row(m, withDate) {
      const r = el('div', 'crow s' + (m.stars || 0) + (m.past ? ' exp' : ''));
      const when = el('div', 'cwhen');
      if (withDate) {
        const d = parseYmd(m.start);
        when.append(el('b', '', (d.getMonth() + 1) + '月' + d.getDate() + '日 周' + WEEK_ZH[d.getDay()]), document.createTextNode(this.timeText(m)));
      } else {
        when.append(el('b', '', this.timeText(m)));
      }
      const sub = el('div', 'csub', m.subject || '(无主题)');
      sub.title = m.subject || '';
      const meta = el('div', 'cmeta');
      const pill = el('span', 'pill');
      const dot = el('i'); dot.style.background = CAT_COLOR[m.cat] || '#888';
      pill.append(dot, el('span', '', m.cat_zh || m.cat || ''));
      meta.append(pill);
      if (m.kind === 'event') { if (m.ev_status !== 'upcoming') { const bg = evBadge(m); if (bg) meta.append(bg); } }
      else if (m.past) meta.append(el('span', 'badge b-expired', '已过期'));
      if (m.kind === 'event' && m.event_place) meta.append(el('span', '', '📍 ' + m.event_place));
      const se = starsEl(m.stars, m.importance);
      if (se) meta.append(se);
      r.append(when, sub, meta);
      r.addEventListener('click', () => openDrawer(m.uid));
      return r;
    },
    renderMonth() {
      const [a] = this.range();
      this.updateTitle();
      const byDay = {};
      for (const m of this.items) if (this.visible(m)) (byDay[m.start.slice(0, 10)] = byDay[m.start.slice(0, 10)] || []).push(m);
      const today = ymd(new Date());
      const cells = [];
      for (let i = 0; i < 42; i++) {
        const d = new Date(a.getFullYear(), a.getMonth(), a.getDate() + i);
        const key = ymd(d);
        const list = byDay[key] || [];
        const cell = el('div', 'cday' + (d.getMonth() !== this.month.getMonth() ? ' other' : '') + (key === today ? ' today' : '') + (key === this.sel ? ' sel' : ''));
        const num = el('div', 'cnum');
        num.append(el('span', '', d.getDate() === 1 ? (d.getMonth() + 1) + '月' + d.getDate() + '日' : String(d.getDate())));
        if (list.length) num.append(el('span', 'cn', list.length + ' 项'));
        cell.append(num);
        for (const m of list.slice(0, 3)) cell.append(this.chip(m));
        if (list.length > 3) cell.append(el('div', 'cmore', '+' + (list.length - 3)));
        cell.addEventListener('click', () => { this.sel = (this.sel === key ? '' : key); this.renderMonth(); });
        cells.push(cell);
      }
      $('#calgrid').replaceChildren(...cells);

      // 选中日的全部条目
      const dp = $('#calday');
      if (this.sel) {
        const list = byDay[this.sel] || [];
        const d = parseYmd(this.sel);
        dp.replaceChildren();
        const h = el('div', 'calph');
        h.append(el('b', '', (d.getMonth() + 1) + '月' + d.getDate() + '日 周' + WEEK_ZH[d.getDay()]), el('span', '', list.length + ' 项'));
        dp.append(h);
        if (!list.length) dp.append(el('div', 'cempty', '这天没有截止或活动'));
        for (const m of list) dp.append(this.row(m, false));
        dp.hidden = false;
      } else {
        dp.hidden = true;
      }

      // 接下来 14 天
      const up = this.upcoming.filter(m => this.visible(m));
      $('#cal-upcount').textContent = up.length + ' 项';
      const box = $('#calup');
      box.replaceChildren();
      if (!up.length) box.append(el('div', 'cempty', '接下来两周没有截止或活动' + ((this.min3 || this.hideExp) ? '（当前有筛选）' : '')));
      for (const m of up) box.append(this.row(m, true));
    },

    // ================= 通用 =================
    shift(n) {
      if (this.mode === 'week') {
        this.weekStart = addDays(this.weekStart, 7 * n);
      } else {
        this.month = new Date(this.month.getFullYear(), this.month.getMonth() + n, 1);
        this.sel = '';
      }
      this.load();
    },
    goToday() {
      const t = new Date();
      if (this.mode === 'week') {
        this.weekStart = mondayOf(t);
        this.scrolledFor = '';
      } else {
        this.month = new Date(t.getFullYear(), t.getMonth(), 1);
        this.sel = ymd(t);
      }
      this.load();
    },
    async exportIcs() {
      const b = $('#cal-export');
      b.disabled = true;
      try {
        const r = await post('/api/calendar/ics', { min: this.min3 ? 3 : 0 });
        toast('已导出 ' + r.count + ' 项邮件事项到 下载/' + r.name + '（不含课程），双击可加入系统日历');
      } catch (e) {
        toast('导出失败：' + e.message, true);
      } finally {
        b.disabled = false;
      }
    },
    bind() {
      $('#cal-prev').onclick = () => this.shift(-1);
      $('#cal-next').onclick = () => this.shift(1);
      $('#cal-today').onclick = () => this.goToday();
      $('#cal-mode').addEventListener('click', e => { const b = e.target.closest('button'); if (b) this.setMode(b.dataset.mode); });
      $('#cal-min3').addEventListener('change', e => { this.min3 = e.target.checked; lsSet('mc_cal_min3', this.min3 ? '1' : '0'); this.render(); });
      $('#cal-hideexp').addEventListener('change', e => { this.hideExp = e.target.checked; lsSet('mc_cal_hideexp', this.hideExp ? '1' : '0'); this.render(); });
      $('#cal-export').onclick = () => this.exportIcs();
    }
  };

  // ------------------------------------------------------------ 抽屉 / 弹窗
  let drawerSeq = 0;      // 打开/关闭一次加一，防止慢请求回来覆盖别的邮件
  const fmtSize = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';
  function fileIcon(a) {
    const ext = ((a.filename.split('.').pop()) || '').toLowerCase(), ct = a.content_type || '';
    if (ct.startsWith('image/')) return '🖼';
    if (ct.startsWith('audio/')) return '🎵';
    if (ct.startsWith('video/')) return '🎬';
    if (ext === 'pdf' || ct === 'application/pdf') return '📕';
    if (['doc', 'docx', 'pages', 'rtf', 'txt', 'md'].includes(ext)) return '📝';
    if (['xls', 'xlsx', 'csv', 'numbers'].includes(ext)) return '📊';
    if (['ppt', 'pptx', 'key'].includes(ext)) return '📽';
    if (['zip', 'rar', '7z', 'gz', 'tar', 'tgz'].includes(ext)) return '🗜';
    if (ext === 'ics' || ct === 'text/calendar') return '📅';
    if (ext === 'eml') return '✉️';
    return '📎';
  }
  // 纯文本里的网址变成可点链接（target=_blank，pywebview 会交给系统浏览器）
  function linkify(text) {
    const frag = document.createDocumentFragment();
    const re = /https?:\/\/[^\s<>"'）】]+/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      const url = m[0].replace(/[.,;:!?)\]]+$/, '');
      if (m.index > last) frag.append(text.slice(last, m.index));
      const a = el('a', '', url);
      a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      frag.append(a);
      last = m.index + url.length;
      re.lastIndex = last;
    }
    frag.append(text.slice(last));
    return frag;
  }
  function noteBar(text, warn, ...btns) {
    const n = el('div', 'dnote' + (warn ? ' warn' : ''));
    n.append(el('span', 'grow', text), ...btns);
    return n;
  }
  function renderAttachments(box, uid, atts, saved) {
    box.replaceChildren();
    if (!atts.length) return;
    const wrap = el('div', 'datts');
    wrap.append(el('div', 'dah', '附件 · ' + atts.length));
    for (const a of atts) {
      const row = el('div', 'datt');
      const info = el('div', 'an');
      const sub = el('span', '', fmtSize(a.size) + ' · ' + (a.content_type || ''));
      info.append(el('b', '', a.filename), sub);
      info.title = a.filename;
      const btn = el('button', '', '保存到下载');
      if (saved[a.index]) { sub.textContent = '已保存到 ' + saved[a.index]; row.classList.add('saved'); btn.textContent = '已保存 ✓'; btn.disabled = true; }
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = '保存中…';
        try {
          const s = await post('/api/email/' + uid + '/attachment/' + a.index + '/save');
          saved[a.index] = s.path;
          sub.textContent = '已保存到 ' + s.path;
          row.classList.add('saved');
          btn.textContent = '已保存 ✓';
          toast('已保存到 ' + s.path);
        } catch (e) {
          btn.disabled = false; btn.textContent = '保存到下载';
          toast('保存失败：' + e.message, true);
        }
      };
      row.append(el('span', 'ai', fileIcon(a)), info, btn);
      wrap.append(row);
    }
    box.append(wrap);
  }
  // 按需取原文并显示：HTML 放沙箱 iframe（无脚本、无同源），否则纯文本；失败退回本地纯文本
  async function loadFull(my, uid, images, ctx) {
    ctx.note.replaceChildren(noteBar('正在从邮箱取原文…'));
    let r;
    try { r = await api('/api/email/' + uid + '/full' + (images ? '?images=1' : '')); } catch (e) { r = { ok: false, error: e.message }; }
    if (my !== drawerSeq) return;
    if (!r.ok) {
      const retry = el('button', '', '重试');
      retry.onclick = () => loadFull(my, uid, images, ctx);
      ctx.note.replaceChildren(noteBar('没能取到邮件原文：' + (r.error || '未知错误') + '。下面显示的是本地保存的纯文本（只存了前一部分）。', true, retry));
      return;
    }
    if (!ctx.metaDone) {
      ctx.metaDone = true;
      if (r.to) ctx.add('收件人', r.to);
      if (r.cc) ctx.add('抄送', r.cc);
    }
    if (r.html) {
      const f = document.createElement('iframe');
      f.className = 'dframe';
      f.setAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox');   // 没有 allow-scripts / allow-same-origin
      f.setAttribute('referrerpolicy', 'no-referrer');
      f.srcdoc = r.html;
      ctx.body.replaceChildren(f);
    } else {
      const d = el('div', 'dbody');
      d.append(r.text && r.text.trim() ? linkify(r.text) : document.createTextNode('（正文为空）'));
      ctx.body.replaceChildren(d);
    }
    if (r.blocked_images > 0) {
      const b = el('button', '', '显示外部图片');
      b.onclick = () => loadFull(my, uid, true, ctx);
      ctx.note.replaceChildren(noteBar('已屏蔽 ' + r.blocked_images + ' 张外部图片（防止发件人追踪你是否打开了邮件）。', false, b));
    } else if (r.external_images) {
      ctx.note.replaceChildren(noteBar('已显示外部图片。'));
    } else {
      ctx.note.replaceChildren();
    }
    renderAttachments(ctx.atts, uid, r.attachments || [], ctx.saved);
  }
  async function openDrawer(uid) {
    const my = ++drawerSeq;
    const dr = $('#drawer'), box = $('#dcontent');
    box.replaceChildren(el('div', 'conf', '加载中…'));
    dr.classList.add('open');
    dr.setAttribute('aria-hidden', 'false');
    $('#drawer-mask').hidden = false;
    try {
      const m = await api('/api/email/' + uid);
      if (my !== drawerSeq) return;
      const meta = el('div', 'dmeta');
      const add = (k, v, node) => { meta.append(el('div', '', k)); const d = el('div'); if (node) d.append(node); else d.textContent = v; meta.append(d); };
      add('日期', m.date || '');
      add('发件人', m.sender || '');
      if (m.cat) {
        const pill = el('span', 'pill');
        const dot = el('i'); dot.style.background = CAT_COLOR[m.cat] || '#888';
        pill.append(dot, el('span', '', (m.cat_zh || m.cat) + ' · ' + Math.round((m.cat_conf || 0) * 100) + '%'));
        add('类别', '', pill);
        const se = starsEl(m.stars, m.importance);
        if (se) {
          const w = el('span');
          w.append(se, document.createTextNode(' ' + STAR_ZH[m.stars] + (m.ignored ? '（发件人在「总是忽略」名单里，没有送去 Jev）' : '')));
          add('重要程度', '', w);
        }
        add('要动手', (m.action >= 0.6 ? '是 ' : '否 ') + Math.round((m.action || 0) * 100) + '%');
        if (m.event_start) {
          const s = el('span');
          s.append(document.createTextNode(fmtEvent(m.event_start, m.event_end) + (m.event_place ? ' · ' + m.event_place : '') + ' · '), evBadge(m));
          s.title = m.event_text ? '邮件原文：' + m.event_text : '';
          add('活动', '', s);
        }
        if (m.deadline) {
          const s = el('span');
          s.append(document.createTextNode(fmtDdl(m.deadline, true) + ' · '), ddlBadge(m));
          s.title = m.deadline_text ? '邮件原文：' + m.deadline_text : '';
          add('截止', '', s);
        }
      }
      const ctx = { add, note: el('div'), atts: el('div'), body: el('div'), metaDone: false, saved: {} };
      ctx.body.append(el('div', 'dbody', m.body || '（正文为空）'));       // 先给本地纯文本，原文取回来再替换
      box.replaceChildren(el('div', 'dsubj', m.subject || '(无主题)'), meta, ctx.note, ctx.atts, ctx.body);
      loadFull(my, uid, false, ctx);
    } catch (e) {
      if (my === drawerSeq) box.replaceChildren(el('div', 'conf', '加载失败：' + e.message));
    }
  }
  function closeDrawer() {
    drawerSeq++;
    $('#drawer').classList.remove('open');
    $('#drawer').setAttribute('aria-hidden', 'true');
    $('#drawer-mask').hidden = true;
    setTimeout(() => { if (!$('#drawer').classList.contains('open')) $('#dcontent').replaceChildren(); }, 300);   // 卸掉 iframe，省内存
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
    $('#st-prefs').value = cfg.preferences || '';
    $('#st-ignore').value = cfg.ignore_senders || '';
    $('#st-error').hidden = true;
    $('#settings-mask').hidden = false;
  }
  function closeSettings() { $('#settings-mask').hidden = true; }

  async function saveSettings() {
    const err = $('#st-error');
    err.hidden = true;
    const body = {
      since: $('#st-since').value, before: $('#st-before').value,
      auto_sync: $('#st-auto').checked, auto_sync_minutes: parseInt($('#st-min').value, 10) || 30,
      preferences: $('#st-prefs').value, ignore_senders: $('#st-ignore').value
    };
    try {
      const cfg = await post('/api/settings', body);
      S.setup = Object.assign(S.setup || {}, cfg, { logged_in: true });
      closeSettings();
      toast('设置已保存；同步范围、我的偏好、忽略名单对之后新分类的邮件生效');
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
    $('#btn-fold').onclick = () => { Cal.autoFolded = false; document.body.classList.toggle('collapsed'); setTimeout(layoutAll, 260); };
    window.addEventListener('resize', () => { if (S.view === 'calendar') Cal.fit(); });
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
        if (S.view === 'crawl' || S.view === 'calendar') { if (!S.q) return; setView('all'); } else { L.reset(); loadList(true); }
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
    $('#l-sort').addEventListener('change', e => { S.sort = e.target.value; L.reset(); loadList(true); });
    $('#l-stars').addEventListener('change', e => {
      const v = e.target.value;   // '' | 'min4' | 'max2' | ...
      S.minStars = v.startsWith('min') ? v.slice(3) : '';
      S.maxStars = v.startsWith('max') ? v.slice(3) : '';
      L.reset(); loadList(true);
    });
    Cal.bind();
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
