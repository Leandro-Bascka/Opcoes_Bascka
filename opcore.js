/* opcore.js - núcleo do app: formatação em pt-BR, matemática de apoio e o motor do gráfico.
   Sem dependências externas (funciona offline depois de carregado). */
(function (global) {
  'use strict';

  /* ------------------------------------------------------------------ formatação */
  const nf2 = new Intl.NumberFormat('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf0 = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 0 });
  const ok = (v) => v !== null && v !== undefined && !Number.isNaN(v);
  const fmt = {
    n2: (v) => (ok(v) ? nf2.format(v) : '–'),
    n0: (v) => (ok(v) ? nf0.format(v) : '–'),
    brl: (v) => (ok(v) ? 'R$ ' + nf2.format(v) : '–'),
    pct: (v, d = 1) => (ok(v) ? v.toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d }) + '%' : '–'),
    spct: (v, d = 1) => (ok(v) ? (v > 0 ? '+' : '') + fmt.pct(v, d) : '–'),
    data: (s) => (s ? s.slice(8, 10) + '/' + s.slice(5, 7) + '/' + s.slice(2, 4) : '–'),
    dataLonga: (s) => (s ? s.slice(8, 10) + '/' + s.slice(5, 7) + '/' + s.slice(0, 4) : '–'),
    dm: (s) => (s ? s.slice(8, 10) + '/' + s.slice(5, 7) : '–'),
    compacto: (v) => {
      if (!ok(v)) return '–';
      const a = Math.abs(v);
      if (a >= 1e9) return (v / 1e9).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + ' bi';
      if (a >= 1e6) return (v / 1e6).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + ' mi';
      if (a >= 1e3) return (v / 1e3).toLocaleString('pt-BR', { maximumFractionDigits: 0 }) + ' mil';
      return nf0.format(v);
    },
    horaBR: (iso) => {
      if (!iso) return '–';
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' });
    },
  };

  /* ------------------------------------------------------------------ matemática */
  function sma(vals, n) {
    const out = new Array(vals.length).fill(null);
    let s = 0;
    for (let i = 0; i < vals.length; i++) {
      s += vals[i];
      if (i >= n) s -= vals[i - n];
      if (i >= n - 1) out[i] = s / n;
    }
    return out;
  }
  /* faixa esperada de preço: preço × vol × √(dias/365), 1 e 2 desvios */
  function faixaEsperada(preco, vol, dias) {
    const sd = vol * Math.sqrt(dias / 365);
    return { sd, m1: preco * (1 - sd), p1: preco * (1 + sd), m2: preco * (1 - 2 * sd), p2: preco * (1 + 2 * sd) };
  }

  /* ------------------------------------------------------------------ gráfico */
  const CAMADAS_PADRAO = {
    mm: true, volume: true, zonas: true, fib: true, fvg: true, ob: true, liquidez: true,
    estrutura: true, swings: false, premdesc: false, faixa: true, alvos: true, rsi: false, macd: false,
  };

  class Chart {
    constructor(canvas) {
      this.cv = canvas;
      this.ctx = canvas.getContext('2d');
      this.a = null;
      this.tf = 'D';
      this.layers = Object.assign({}, CAMADAS_PADRAO);
      this.dte = 30;
      this.linhas = []; // linhas extras: {p, label, cor, tracejado, forcar}
      this.nVis = 110;
      this.off = 0;
      this.pad = 8;
      this.hover = null;
      this.S = null;
      this._ptr = new Map();
      this._moved = 0;
      this._bind();
      if (typeof ResizeObserver !== 'undefined') {
        this._ro = new ResizeObserver(() => this.draw());
        this._ro.observe(canvas);
      }
    }

    setAtivo(a) { this.a = a; this.hover = null; this.off = 0; this._series(); this.draw(); }
    setTf(tf) { this.tf = tf; this.hover = null; this.off = 0; this._series(); this.draw(); }
    setLayers(l) { Object.assign(this.layers, l); this.draw(); }
    setDte(d) { this.dte = d; this.draw(); }
    setLinhas(l) { this.linhas = l || []; this.draw(); }
    zoom(f) {
      this.nVis = Math.max(25, Math.min(this.S ? this.S.c.length + 10 : 400, Math.round(this.nVis * f)));
      this._clamp(); this.draw();
    }

    _series() {
      const a = this.a;
      if (!a) { this.S = null; return; }
      if (this.tf === 'W') {
        const c = a.semanal;
        const mm = sma(c.map((k) => k[4]), 20);
        this.S = { c, isD: false, mm: [{ nome: 'MM20', cor: '--ma21', v: mm }] };
      } else {
        const i = a.ind;
        const mm = [];
        for (const p of [21, 50, 200]) if (i['mm' + p]) mm.push({ nome: 'MM' + p, cor: '--ma' + p, v: i['mm' + p] });
        this.S = { c: a.candles, isD: true, mm };
      }
    }

    _clamp() {
      if (!this.S) return;
      const max = Math.max(0, this.S.c.length - 1 + this.pad - this.nVis + 5);
      this.off = Math.max(0, Math.min(max, this.off));
    }

    _bind() {
      const cv = this.cv;
      cv.addEventListener('pointerdown', (e) => {
        cv.setPointerCapture && cv.setPointerCapture(e.pointerId);
        this._ptr.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX });
        this._moved = 0;
        if (this._ptr.size === 2) this._pd = this._dist();
      });
      cv.addEventListener('pointermove', (e) => {
        const p = this._ptr.get(e.pointerId);
        if (!p || !this.S) return;
        const dx = e.clientX - p.x;
        p.x = e.clientX; p.y = e.clientY;
        this._moved += Math.abs(dx);
        if (this._ptr.size === 1) {
          const barW = this._plotW / this.nVis;
          this.off += dx / barW;
          this._clamp(); this.draw();
        } else if (this._ptr.size === 2) {
          const d = this._dist();
          if (this._pd && d) { this.zoom(this._pd / d); }
          this._pd = d;
        }
      });
      const fim = (e) => {
        const p = this._ptr.get(e.pointerId);
        if (p && this._ptr.size === 1 && this._moved < 6 && this.S) this._toque(e);
        this._ptr.delete(e.pointerId);
        this._pd = null;
      };
      cv.addEventListener('pointerup', fim);
      cv.addEventListener('pointercancel', (e) => { this._ptr.delete(e.pointerId); this._pd = null; });
      cv.addEventListener('wheel', (e) => { e.preventDefault(); this.zoom(e.deltaY > 0 ? 1.12 : 0.89); }, { passive: false });
    }
    _dist() {
      const pts = [...this._ptr.values()];
      return pts.length === 2 ? Math.abs(pts[0].x - pts[1].x) : 0;
    }
    _toque(e) {
      const r = this.cv.getBoundingClientRect();
      const x = e.clientX - r.left;
      const i = Math.round(this._L + (x - this._plotL) / (this._plotW / this.nVis) - 0.5);
      if (i < 0 || i >= this.S.c.length) { this.hover = null; } else { this.hover = this.hover === i ? null : i; }
      this.draw();
    }

    /* cores vêm das variáveis CSS (acompanham o tema claro/escuro) */
    _cor(n) { return this._css.getPropertyValue(n).trim() || '#888'; }

    draw() {
      const cv = this.cv, ctx = this.ctx;
      const dpr = global.devicePixelRatio || 1;
      const W = cv.clientWidth || 640;
      const Ly = this.layers;
      const S = this.S;
      this._css = getComputedStyle(cv);
      const diario = !!(S && S.isD); // RSI e MACD existem só no diário
      const hP = 360, hR = Ly.rsi && diario ? 90 : 0, hM = Ly.macd && diario ? 90 : 0, gap = 10, hX = 22;
      const H = hP + (hR ? hR + gap : 0) + (hM ? hM + gap : 0) + hX;
      cv.style.height = H + 'px';
      cv.width = Math.round(W * dpr);
      cv.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (!S || !S.c.length) return;

      const ink = this._cor('--ink'), muted = this._cor('--muted'), line = this._cor('--line');
      const up = this._cor('--up'), down = this._cor('--down'), accent = this._cor('--accent');
      const warn = this._cor('--warn'), zone = this._cor('--zone'), card = this._cor('--card');
      const axisW = 58;
      const plotL = 4, plotR = W - axisW, plotW = plotR - plotL;
      this._plotL = plotL; this._plotW = plotW;
      const n = S.c.length;
      const R = n - 1 + this.pad - this.off;
      const Lx = R - this.nVis;
      this._L = Lx;
      const barW = plotW / this.nVis;
      const X = (i) => plotL + (i - Lx + 0.5) * barW;
      const i0 = Math.max(0, Math.floor(Lx)), i1 = Math.min(n - 1, Math.ceil(R));
      const dIdx = (i) => (this.tf === 'D' ? i : null);

      /* escala de preço */
      let lo = Infinity, hi = -Infinity;
      for (let i = i0; i <= i1; i++) { lo = Math.min(lo, S.c[i][3]); hi = Math.max(hi, S.c[i][2]); }
      const spot = this.a.ultimo.close;
      const forcados = [];
      for (const l of this.linhas) if (l.forcar) forcados.push(l.p);
      const vol = (this.a.vol.hist60 || this.a.vol.hist20) || 0.35;
      const fx = faixaEsperada(spot, vol, this.dte);
      if (Ly.faixa) forcados.push(fx.m1);
      if (Ly.alvos && this.a.alvos[0]) forcados.push(this.a.alvos[0].p);
      const lo0 = lo, hi0 = hi;
      for (const p of forcados) if (p > lo0 * 0.72 && p < hi0 * 1.28) { lo = Math.min(lo, p); hi = Math.max(hi, p); }
      const padv = (hi - lo) * 0.04 || 0.5;
      lo -= padv; hi += padv;
      const volH = Ly.volume ? hP * 0.13 : 0;
      const top = 6, bot = hP - 6;
      const Y = (p) => top + (hi - p) / (hi - lo) * (bot - top);
      const inRange = (p) => p >= lo && p <= hi;

      const txt = (t, x, y, cor, al, f) => {
        ctx.font = f || '11px system-ui, sans-serif'; ctx.textAlign = al || 'left'; ctx.textBaseline = 'middle'; ctx.fillStyle = cor; ctx.fillText(t, x, y);
      };
      const rotulos = []; // rótulos são desenhados por último, por cima dos candles
      const tag = (...args) => { rotulos.push(args); };
      const desenhaTag = (t, x, y, cor, al) => {
        ctx.font = '10.5px system-ui, sans-serif';
        const w = ctx.measureText(t).width + 8;
        const x0 = al === 'right' ? x - w : x;
        ctx.globalAlpha = 0.88; ctx.fillStyle = card; ctx.fillRect(x0, y - 7, w, 14); ctx.globalAlpha = 1;
        ctx.fillStyle = cor; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(t, x0 + 4, y);
      };
      const hline = (p, x0, cor, dash, label, al) => {
        if (!inRange(p)) return;
        const y = Y(p);
        ctx.save(); ctx.strokeStyle = cor; ctx.lineWidth = 1; ctx.setLineDash(dash || []);
        ctx.beginPath(); ctx.moveTo(Math.max(plotL, x0), y); ctx.lineTo(plotR, y); ctx.stroke(); ctx.restore();
        if (label) tag(label, al === 'left' ? Math.max(plotL, x0) + 2 : plotR - 2, y, cor, al === 'left' ? 'left' : 'right');
      };
      const hband = (blo, bhi, x0, cor, alpha, label, bordas) => {
        const a = Math.max(blo, lo), b = Math.min(bhi, hi);
        if (a > b && !(blo >= lo && bhi <= hi)) return;
        const y1 = Y(Math.min(bhi, hi)), y2 = Y(Math.max(blo, lo));
        const xs = Math.max(plotL, x0);
        if (xs >= plotR) return;
        ctx.globalAlpha = alpha; ctx.fillStyle = cor; ctx.fillRect(xs, y1, plotR - xs, Math.max(1.5, y2 - y1)); ctx.globalAlpha = 1;
        if (bordas) { ctx.strokeStyle = cor; ctx.globalAlpha = 0.55; ctx.lineWidth = 1; ctx.strokeRect(xs + 0.5, y1, plotR - xs - 1, Math.max(1.5, y2 - y1)); ctx.globalAlpha = 1; }
        if (label) tag(label, xs + 2, (y1 + y2) / 2, cor, 'left');
      };

      /* grade e eixo de preço */
      ctx.lineWidth = 1;
      const passos = 6;
      for (let k = 0; k <= passos; k++) {
        const p = lo + (hi - lo) * k / passos;
        const y = Y(p);
        ctx.strokeStyle = line; ctx.globalAlpha = 0.7; ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke(); ctx.globalAlpha = 1;
        txt(fmt.n2(p), W - axisW + 6, y, muted, 'left');
      }

      /* premium / desconto */
      if (Ly.premdesc && this.a.smc.range) {
        const [rlo, rhi] = this.a.smc.range, eq = this.a.smc.eq;
        hband(eq, rhi, plotL, down, 0.06, 'Premium', false);
        hband(rlo, eq, plotL, up, 0.06, 'Desconto', false);
        hline(eq, plotL, muted, [4, 4], 'EQ 50%', 'left');
      }
      /* zonas de confluência */
      if (Ly.zonas) {
        for (const z of this.a.zonas.slice(0, 6)) {
          if (z.nota < 3) continue;
          hband(z.lo, z.hi + (z.hi === z.lo ? spot * 0.0012 : 0), plotL, zone, 0.10 + Math.min(0.14, z.nota * 0.02),
            'Zona ' + String(z.nota).replace('.', ','), true);
        }
      }
      /* FVG e order blocks (só diário) */
      if (S.isD && Ly.fvg) {
        for (const f of this.a.smc.fvg) hband(f.lo, f.hi, X(f.i), f.dir === 'alta' ? up : down, 0.16, 'FVG', false);
      }
      if (S.isD && Ly.ob) {
        for (const b of this.a.smc.ob) hband(b.lo, b.hi, X(b.i), b.dir === 'alta' ? up : down, 0.22, 'OB', true);
      }
      /* liquidez */
      if (S.isD && Ly.liquidez) {
        for (const s of this.a.smc.liquidez) hline(s.p, X(s.i), warn, [2, 3], s.igual ? 'Liq. igual' : null, 'left');
      }
      /* Fibonacci */
      if (Ly.fib && this.a.fib) {
        const f = this.a.fib;
        const x0 = S.isD ? X(f.iL) : plotL;
        for (const nv of f.niveis) hline(nv.p, x0, accent, [5, 4], (nv.r * 100).toFixed(1).replace('.', ',') + '%', 'left');
        hline(f.H, x0, muted, [1, 3], 'Topo', 'left');
        hline(f.L, x0, muted, [1, 3], 'Fundo', 'left');
      }

      /* candles */
      for (let i = i0; i <= i1; i++) {
        const k = S.c[i], x = X(i);
        const alta = k[4] >= k[1];
        const cor = alta ? up : down;
        ctx.strokeStyle = cor; ctx.fillStyle = cor; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, Y(k[2])); ctx.lineTo(x, Y(k[3])); ctx.stroke();
        const yo = Y(k[1]), yc = Y(k[4]);
        const bw = Math.max(1, barW * 0.68);
        ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
      }
      /* volume */
      if (Ly.volume) {
        let vm = 0;
        for (let i = i0; i <= i1; i++) vm = Math.max(vm, S.c[i][5]);
        for (let i = i0; i <= i1 && vm > 0; i++) {
          const k = S.c[i], h = volH * k[5] / vm;
          ctx.globalAlpha = 0.28; ctx.fillStyle = k[4] >= k[1] ? up : down;
          ctx.fillRect(X(i) - Math.max(1, barW * 0.68) / 2, bot - h, Math.max(1, barW * 0.68), h);
          ctx.globalAlpha = 1;
        }
      }
      /* médias móveis */
      if (Ly.mm) {
        for (const m of S.mm) {
          ctx.strokeStyle = this._cor(m.cor); ctx.lineWidth = 1.4; ctx.beginPath();
          let aberto = false;
          for (let i = i0; i <= i1; i++) {
            const v = m.v[i];
            if (v === null || v === undefined) { aberto = false; continue; }
            const x = X(i), y = Y(v);
            if (!aberto) { ctx.moveTo(x, y); aberto = true; } else ctx.lineTo(x, y);
          }
          ctx.stroke();
        }
      }
      /* swings e estrutura (BOS / CHoCH) */
      if (S.isD && Ly.swings) {
        for (const s of this.a.smc.swings) {
          if (s.i < i0 || s.i > i1) continue;
          const x = X(s.i), y = Y(s.p) + (s.k === 'H' ? -6 : 6);
          ctx.fillStyle = s.k === 'H' ? down : up;
          ctx.beginPath();
          if (s.k === 'H') { ctx.moveTo(x, y + 4); ctx.lineTo(x - 3.5, y - 2); ctx.lineTo(x + 3.5, y - 2); } else { ctx.moveTo(x, y - 4); ctx.lineTo(x - 3.5, y + 2); ctx.lineTo(x + 3.5, y + 2); }
          ctx.fill();
        }
      }
      if (S.isD && Ly.estrutura) {
        for (const e of this.a.smc.eventos) {
          if (e.i < i0 || e.i > i1 + 1 || !inRange(e.nivel)) continue;
          const cor = e.dir === 'alta' ? up : down;
          const y = Y(e.nivel), xa = Math.max(plotL, X(e.origem)), xb = X(e.i);
          ctx.save(); ctx.strokeStyle = cor; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(xa, y); ctx.lineTo(xb, y); ctx.stroke(); ctx.restore();
          txt(e.tipo, (xa + xb) / 2, y + (e.dir === 'alta' ? -7 : 8), cor, 'center', '600 10px system-ui, sans-serif');
        }
      }
      /* faixa esperada até o vencimento */
      if (Ly.faixa) {
        const xs = X(n - 1);
        hband(fx.m2, fx.m1, xs, down, 0.07, null, false);
        hline(fx.m1, xs, down, [6, 4], '−1σ ' + this.dte + 'd ' + fmt.n2(fx.m1), 'right');
        hline(fx.m2, xs, down, [2, 3], '−2σ ' + fmt.n2(fx.m2), 'right');
        hline(fx.p1, xs, up, [6, 4], '+1σ ' + fmt.n2(fx.p1), 'right');
      }
      /* alvos das CALLs */
      if (Ly.alvos) {
        this.a.alvos.forEach((al, k) => hline(al.p, plotL, up, [8, 4], 'Alvo ' + (k + 1) + ' ' + fmt.n2(al.p), 'left'));
      }
      /* linhas extras (strike, equilíbrio, entrada...) */
      for (const l of this.linhas) {
        const cor = l.cor === 'down' ? down : l.cor === 'up' ? up : l.cor === 'warn' ? warn : accent;
        hline(l.p, plotL, cor, l.tracejado ? [3, 3] : [], l.label + ' ' + fmt.n2(l.p), 'left');
      }
      for (const r of rotulos) desenhaTag(...r);
      /* preço atual */
      {
        const y = Y(spot);
        ctx.save(); ctx.strokeStyle = ink; ctx.globalAlpha = 0.5; ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke(); ctx.restore();
        ctx.fillStyle = ink; ctx.fillRect(plotR + 1, y - 8, axisW - 2, 16);
        txt(fmt.n2(spot), plotR + 5, y, card, 'left', '600 11px system-ui, sans-serif');
      }
      /* moldura do painel de preço */
      ctx.strokeStyle = line; ctx.strokeRect(plotL + 0.5, 0.5, plotW - 1, hP - 1);

      /* painéis RSI / MACD */
      let yPanel = hP + gap;
      const painel = (altura, titulo, desenha) => {
        ctx.strokeStyle = line; ctx.strokeRect(plotL + 0.5, yPanel + 0.5, plotW - 1, altura - 1);
        txt(titulo, plotL + 6, yPanel + 9, muted, 'left', '600 10.5px system-ui, sans-serif');
        desenha(yPanel, altura);
        yPanel += altura + gap;
      };
      if (hR && S.isD) {
        painel(hR, 'RSI 14', (y0, h) => {
          const Yr = (v) => y0 + 4 + (100 - v) / 100 * (h - 8);
          for (const nv of [30, 70]) {
            ctx.strokeStyle = line; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(plotL, Yr(nv)); ctx.lineTo(plotR, Yr(nv)); ctx.stroke(); ctx.setLineDash([]);
            txt(String(nv), W - axisW + 6, Yr(nv), muted, 'left');
          }
          ctx.strokeStyle = accent; ctx.lineWidth = 1.4; ctx.beginPath(); let ab = false;
          for (let i = i0; i <= i1; i++) {
            const v = this.a.ind.rsi[i];
            if (v === null || v === undefined) { ab = false; continue; }
            if (!ab) { ctx.moveTo(X(i), Yr(v)); ab = true; } else ctx.lineTo(X(i), Yr(v));
          }
          ctx.stroke();
        });
      } else if (hR) { yPanel += hR + gap; }
      if (hM && S.isD) {
        painel(hM, 'MACD 12,26,9', (y0, h) => {
          let mx = 0;
          for (let i = i0; i <= i1; i++) for (const arr of [this.a.ind.macd, this.a.ind.sinal, this.a.ind.hist]) if (arr[i] !== null) mx = Math.max(mx, Math.abs(arr[i]));
          mx = mx || 1;
          const Ym = (v) => y0 + h / 2 - v / mx * (h / 2 - 5);
          ctx.strokeStyle = line; ctx.beginPath(); ctx.moveTo(plotL, Ym(0)); ctx.lineTo(plotR, Ym(0)); ctx.stroke();
          for (let i = i0; i <= i1; i++) {
            const v = this.a.ind.hist[i];
            if (v === null || v === undefined) continue;
            ctx.globalAlpha = 0.5; ctx.fillStyle = v >= 0 ? up : down;
            ctx.fillRect(X(i) - Math.max(1, barW * 0.6) / 2, Math.min(Ym(0), Ym(v)), Math.max(1, barW * 0.6), Math.abs(Ym(v) - Ym(0)));
            ctx.globalAlpha = 1;
          }
          for (const [arr, cor] of [[this.a.ind.macd, accent], [this.a.ind.sinal, warn]]) {
            ctx.strokeStyle = cor; ctx.lineWidth = 1.3; ctx.beginPath(); let ab = false;
            for (let i = i0; i <= i1; i++) {
              const v = arr[i];
              if (v === null || v === undefined) { ab = false; continue; }
              if (!ab) { ctx.moveTo(X(i), Ym(v)); ab = true; } else ctx.lineTo(X(i), Ym(v));
            }
            ctx.stroke();
          }
        });
      } else if (hM) { yPanel += hM + gap; }

      /* eixo de datas */
      const yd = H - hX / 2 - 1;
      const marcas = 5;
      for (let k = 0; k <= marcas; k++) {
        const i = Math.round(i0 + (i1 - i0) * k / marcas);
        if (S.c[i]) txt(fmt.dm(S.c[i][0]) + '/' + S.c[i][0].slice(2, 4), Math.min(Math.max(X(i), plotL + 22), plotR - 22), yd, muted, 'center');
      }

      /* cursor e informações do candle tocado */
      if (this.hover !== null && S.c[this.hover]) {
        const i = this.hover, k = S.c[i], x = X(i);
        ctx.save(); ctx.strokeStyle = ink; ctx.globalAlpha = 0.5; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, hP); ctx.stroke(); ctx.restore();
        const linhas = [fmt.dataLonga(k[0]), 'A ' + fmt.n2(k[1]) + '  M ' + fmt.n2(k[2]), 'm ' + fmt.n2(k[3]) + '  F ' + fmt.n2(k[4]), 'Vol ' + fmt.compacto(k[5])];
        if (S.isD && this.a.ind.rsi[i] !== null) linhas.push('RSI ' + fmt.n2(this.a.ind.rsi[i]));
        ctx.font = '11px system-ui, sans-serif';
        const bw = Math.max(...linhas.map((t) => ctx.measureText(t).width)) + 14, bh = linhas.length * 15 + 8;
        const bx = x > plotW / 2 ? plotL + 8 : plotR - bw - 8;
        ctx.globalAlpha = 0.95; ctx.fillStyle = card; ctx.fillRect(bx, 8, bw, bh); ctx.globalAlpha = 1;
        ctx.strokeStyle = line; ctx.strokeRect(bx + 0.5, 8.5, bw, bh);
        linhas.forEach((t, j) => txt(t, bx + 7, 8 + 12 + j * 15, j === 0 ? ink : muted, 'left'));
      }
    }
  }

  global.OpcCore = { fmt, sma, faixaEsperada, Chart, CAMADAS_PADRAO };
})(typeof window !== 'undefined' ? window : globalThis);
