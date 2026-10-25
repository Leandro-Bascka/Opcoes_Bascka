/* opcoes.js - lógica do app: carrega os JSON gerados pelo GitHub Actions, mostra alertas ao abrir,
   gráfico com todas as camadas e as tabelas de ranking com a observação de cada linha. */
(function () {
  'use strict';
  const { fmt, Chart, CAMADAS_PADRAO } = window.OpcCore;

  const $ = (s, el) => (el || document).querySelector(s);
  function h(tag, props, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return e;
  }

  const S = { meta: null, uni: null, alertas: null, quedas: null, opt: null, ativos: {}, cur: null, tf: 'D', dte: 30, v: '' };
  let chart = null;

  const ABAS = [
    ['grafico', 'Gráfico'], ['puts', 'PUTs'], ['calls', 'Entradas CALL'],
    ['quedas', 'Quedas e CALLs longas'], ['posicoes', 'Posições'], ['sobre', 'Sobre'],
  ];
  const CAM_ROTULOS = {
    mm: 'Médias', volume: 'Volume', zonas: 'Zonas', fib: 'Fibonacci', fvg: 'FVG', ob: 'Order block',
    liquidez: 'Liquidez', estrutura: 'BOS/CHoCH', swings: 'Swings', premdesc: 'Premium/desconto',
    faixa: 'Faixa de preço', alvos: 'Alvos', rsi: 'RSI', macd: 'MACD',
  };

  async function getJSON(caminho) {
    const r = await fetch(caminho + '?v=' + encodeURIComponent(S.v || Date.now()), { cache: 'no-cache' });
    if (!r.ok) throw new Error(caminho + ' (' + r.status + ')');
    return r.json();
  }
  async function getAtivo(t) {
    if (!S.ativos[t]) S.ativos[t] = await getJSON('data/ativos/' + t + '.json');
    return S.ativos[t];
  }

  /* ------------------------------------------------------------------ tabela genérica */
  function tabela(cols, linhas, opc) {
    opc = opc || {};
    let ordem = { i: null, asc: false };
    const tb = h('tbody');
    const cab = h('tr');
    const render = () => {
      tb.replaceChildren();
      let ls = linhas.slice();
      if (ordem.i !== null) {
        const c = cols[ordem.i];
        ls.sort((a, b) => {
          const va = c.sort ? c.sort(a) : null, vb = c.sort ? c.sort(b) : null;
          if (va === null || va === undefined) return 1;
          if (vb === null || vb === undefined) return -1;
          return (va < vb ? -1 : va > vb ? 1 : 0) * (ordem.asc ? 1 : -1);
        });
      }
      for (const r of ls) {
        const det = opc.detalhe ? h('tr', { class: 'det hidden' }, h('td', { colspan: cols.length }, opc.detalhe(r))) : null;
        const tr = h('tr', { class: 'dados', onclick: () => det && det.classList.toggle('hidden') },
          cols.map((c) => h('td', { class: (c.l ? 'l ' : '') + (c.cls ? c.cls(r) : 'n') }, c.v(r))));
        tb.append(tr);
        if (det) tb.append(det);
      }
    };
    cols.forEach((c, i) => cab.append(h('th', {
      class: c.l ? 'l' : '', title: c.dica || '',
      onclick: () => { if (!c.sort) return; ordem = { i, asc: ordem.i === i ? !ordem.asc : false }; render(); },
    }, c.t)));
    render();
    return h('div', { class: 'tabela-wrap' }, h('table', null, h('thead', null, cab), tb));
  }
  const pill = (t, tipo) => h('span', { class: 'pill ' + (tipo || '') }, t);

  /* ------------------------------------------------------------------ cabeçalho e alertas */
  function pintaStatus() {
    const m = S.meta;
    const el = $('#status');
    el.replaceChildren();
    if (!m) { el.textContent = 'sem dados'; return; }
    el.append('Pregão ', h('b', null, fmt.dataLonga(m.ultimo_pregao)), h('br'), 'atualizado ', fmt.horaBR(m.gerado_em));
    const dias = (Date.now() - new Date(m.ultimo_pregao + 'T12:00:00').getTime()) / 864e5;
    $('#avisos').replaceChildren();
    if (dias > 4.5) {
      $('#avisos').append(h('div', { class: 'aviso' }, 'Os dados têm ', Math.floor(dias), ' dias. Confira se o workflow "Atualizar dados" está rodando na aba Actions do GitHub.'));
    }
  }

  function pintaAlertas() {
    const box = $('#alertas');
    box.replaceChildren();
    const a = S.alertas;
    if (!a) return;
    const corte = S.meta && S.meta.config ? S.meta.config.zonas : null;
    box.append(h('h2', null, 'Ativos em zona de interesse'));
    if (!a.itens.length) {
      box.append(h('div', { class: 'vazio' }, 'Nenhum dos ' + (S.uni ? S.uni.itens.length : 25) + ' ativos está em zona de interesse agora' + (corte ? ' (nota mínima ' + fmt.n2(corte) + ').' : '.')));
      return;
    }
    box.append(h('div', { class: 'alertas-lista' }, a.itens.map((x) => h('button', { class: 'alerta', onclick: () => abrirGrafico(x.ticker) },
      h('div', { class: 'alerta-topo' }, h('span', { class: 'tk' }, x.ticker), h('span', { class: 'nota' }, 'nota ' + fmt.n2(x.nota))),
      h('div', { class: 'zona num' }, 'Zona ' + fmt.n2(x.zona[0]) + ' – ' + fmt.n2(x.zona[1]) + ' · preço ' + fmt.n2(x.preco)),
      h('p', null, x.fatores.join(' · ')),
      x.gatilhos.length ? h('p', null, 'Gatilhos: ' + x.gatilhos.join(', ')) : null,
    ))));
  }

  /* ------------------------------------------------------------------ abas */
  function mostraAba(id) {
    for (const [a] of ABAS) {
      $('#aba-' + a).setAttribute('aria-selected', String(a === id));
      $('#sec-' + a).classList.toggle('hidden', a !== id);
    }
    $('#alertas').classList.toggle('hidden', id !== 'grafico');
    if (id === 'grafico' && chart) chart.draw();
    history.replaceState(null, '', '#' + id);
  }

  /* ------------------------------------------------------------------ gráfico */
  async function abrirGrafico(ticker, linhas, dte) {
    mostraAba('grafico');
    $('#sel-ativo').value = ticker;
    await carregaAtivo(ticker);
    if (dte) { S.dte = dte; $('#dte').value = dte; $('#dte-v').textContent = dte + ' dias'; chart.setDte(dte); }
    chart.setLinhas(linhas || []);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function carregaAtivo(t) {
    const box = $('#graf-msg');
    try {
      const a = await getAtivo(t);
      S.cur = a;
      box.classList.add('hidden');
      chart.setAtivo(a);
      chart.setLinhas([]);
      pintaDetalhes(a);
    } catch (e) {
      box.textContent = 'Não foi possível carregar ' + t + ': ' + e.message;
      box.classList.remove('hidden');
    }
  }

  function pintaDetalhes(a) {
    const c = a.contexto;
    const alvo = $('#detalhes');
    alvo.replaceChildren();
    const ctx = h('div', { class: 'card' }, h('h2', null, a.ticker + ' · ' + a.nome),
      h('dl', { class: 'kv' },
        h('dt', null, 'Último fechamento'), h('dd', null, fmt.brl(a.ultimo.close) + ' (' + fmt.spct(a.ultimo.var_dia_pct, 2) + ')'),
        h('dt', null, 'Estrutura (diário)'), h('dd', null, c.estrutura),
        h('dt', null, 'Tendência semanal'), h('dd', null, c.semanal),
        h('dt', null, 'Retração desde o topo'), h('dd', null, fmt.pct(c.retr_pct, 0) + (c.em_retracao ? ' (em retração)' : '')),
        h('dt', null, 'RSI 14'), h('dd', null, fmt.n2(c.rsi)),
        h('dt', null, 'ATR / preço'), h('dd', null, fmt.pct(c.atr_pct, 1)),
        h('dt', null, 'Volatilidade histórica 20 / 60 d'), h('dd', null, fmt.pct((a.vol.hist20 || 0) * 100, 0) + ' / ' + fmt.pct((a.vol.hist60 || 0) * 100, 0)),
        h('dt', null, 'Queda desde a máx. de 52 sem.'), h('dd', null, fmt.pct(a.queda.da_max_52s_pct, 1)),
        h('dt', null, 'Liquidez (negócios/dia · volume/dia)'), h('dd', null, fmt.n0(a.liquidez.neg_med) + ' · R$ ' + fmt.compacto(a.liquidez.vol_med))),
      a.gatilhos.length ? h('div', { class: 'chips', style: 'margin-top:8px' }, a.gatilhos.map((g) => h('span', { class: 'chip info' }, g))) : null);
    const zonas = h('div', { class: 'card' }, h('h2', null, 'Zonas de confluência (por nota)'),
      a.zonas.filter((z) => z.nota >= 3).length ? a.zonas.filter((z) => z.nota >= 3).slice(0, 6).map((z) => h('div', { class: 'zona-item' },
        h('div', { class: 'cab' }, h('span', { class: 'num' }, fmt.n2(z.lo) + ' – ' + fmt.n2(z.hi)), h('span', { class: 'linha' }, z.na_zona ? pill('preço na zona', 'ok') : null, h('span', { class: 'nota' }, 'nota ' + fmt.n2(z.nota)))),
        h('div', { class: 'fat' }, z.fatores.join(' · ') + ' · ' + (z.dist_pct >= 0 ? fmt.pct(z.dist_pct) + ' abaixo do preço' : fmt.pct(-z.dist_pct) + ' acima do preço'))))
        : h('div', { class: 'muted' }, 'Nenhuma zona com nota 3 ou mais perto do preço.'),
      h('h2', { style: 'margin-top:12px' }, 'Alvos para CALL'),
      a.alvos.length ? a.alvos.map((x, k) => h('div', { class: 'zona-item' }, h('div', { class: 'cab' }, h('span', null, 'Alvo ' + (k + 1) + ' · ' + x.rot), h('span', { class: 'num' }, fmt.brl(x.p) + ' (' + fmt.spct((x.p / a.ultimo.close - 1) * 100) + ')'))))
        : h('div', { class: 'muted' }, 'Sem alvos acima do preço.'));
    alvo.append(h('div', { class: 'grade2' }, ctx, zonas));
  }

  function montaGrafico() {
    const sec = $('#sec-grafico');
    const sel = h('select', { id: 'sel-ativo', 'aria-label': 'Ativo', onchange: (e) => carregaAtivo(e.target.value) },
      (S.uni ? S.uni.itens : []).map((i) => h('option', { value: i.ticker }, i.ticker + ' · ' + i.nome)));
    const tfBtns = ['D', 'W'].map((tf) => h('button', { 'aria-pressed': String(tf === 'D'), onclick: (e) => {
      S.tf = tf; chart.setTf(tf);
      e.target.parentElement.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === e.target)));
    } }, tf === 'D' ? 'Diário' : 'Semanal'));
    const chips = Object.keys(CAM_ROTULOS).map((k) => {
      const ch = h('span', { class: 'chip' + (CAMADAS_PADRAO[k] ? ' on' : ''), role: 'button', tabindex: '0', onclick: () => {
        const on = !ch.classList.contains('on');
        ch.classList.toggle('on', on);
        chart.setLayers({ [k]: on });
      } }, CAM_ROTULOS[k]);
      return ch;
    });
    sec.append(
      h('div', { class: 'card' },
        h('div', { class: 'linha' }, sel, h('div', { class: 'seg' }, tfBtns),
          h('div', { class: 'seg' }, h('button', { onclick: () => chart.zoom(0.8), 'aria-label': 'Aproximar' }, '＋'), h('button', { onclick: () => chart.zoom(1.25), 'aria-label': 'Afastar' }, '－')),
          h('label', { class: 'range' }, 'Faixa até o vencimento', h('input', { id: 'dte', type: 'range', min: '15', max: '50', value: String(S.dte), oninput: (e) => { S.dte = +e.target.value; $('#dte-v').textContent = S.dte + ' dias'; chart.setDte(S.dte); } }), h('b', { id: 'dte-v' }, S.dte + ' dias'))),
        h('div', { class: 'chips', style: 'margin-top:10px' }, chips)),
      h('div', { class: 'graf-wrap' }, h('div', { id: 'graf-msg', class: 'graf-vazio hidden' }), h('canvas', { id: 'grafico', role: 'img', 'aria-label': 'Gráfico do ativo com camadas de análise' }),
        h('div', { class: 'legenda' },
          h('span', null, h('i', { style: 'background:var(--ma21)' }), 'MM21'), h('span', null, h('i', { style: 'background:var(--ma50)' }), 'MM50'), h('span', null, h('i', { style: 'background:var(--ma200)' }), 'MM200'),
          h('span', null, 'Arraste para mover · pince ou use ＋/－ para o zoom · toque num candle para ver os valores'))),
      h('div', { id: 'detalhes' }));
    chart = new Chart($('#grafico'));
  }

  /* ------------------------------------------------------------------ PUTs */
  function montaPuts() {
    const sec = $('#sec-puts');
    const o = S.opt;
    sec.replaceChildren();
    if (!o) { sec.append(h('div', { class: 'vazio' }, 'Dados de opções ainda não gerados.')); return; }
    const f = o.puts.filtros;
    sec.append(h('div', { class: 'card' }, h('h2', null, 'PUTs para vender'),
      h('div', { class: 'muted' }, 'Ordenadas pelo retorno anualizado sobre a garantia. Só entram séries com ' + f.dte_min + ' a ' + f.dte_max + ' dias, chance de exercício até ' + fmt.pct(f.prob_max * 100, 0) + ', spread até ' + fmt.pct(f.spread_max_pct, 0) + ' e ao menos ' + f.negocios_min_10p + ' negócios nos últimos 10 pregões. Toque numa linha para ver a observação.')));
    if (!o.puts.itens.length) {
      sec.append(h('div', { class: 'vazio' }, 'Nenhuma PUT passou por todos os filtros nos dados do último pregão.'));
    } else {
      sec.append(tabela([
        { t: '#', v: (r) => r.rank, sort: (r) => r.rank, l: true },
        { t: 'Ativo', v: (r) => h('span', { class: 'tk' }, r.ticker), l: true, sort: (r) => r.ticker },
        { t: 'Série', v: (r) => r.serie, l: true },
        { t: 'Strike', v: (r) => fmt.n2(r.strike), sort: (r) => r.strike },
        { t: 'Venc.', v: (r) => fmt.dm(r.venc) + ' · ' + r.dte + 'd', sort: (r) => r.dte },
        { t: 'Prêmio', v: (r) => fmt.n2(r.premio), sort: (r) => r.premio },
        { t: 'Ret. a.a.', v: (r) => fmt.pct(r.ret_anual_pct, 0), sort: (r) => r.ret_anual_pct, dica: 'Retorno anualizado (composto) do prêmio sobre o strike' },
        { t: 'Ret. período', v: (r) => fmt.pct(r.ret_pct, 2), sort: (r) => r.ret_pct },
        { t: 'Chance exerc.', v: (r) => fmt.pct(r.prob_ex * 100, 0), sort: (r) => r.prob_ex },
        { t: 'Dist.', v: (r) => fmt.pct(r.dist_pct, 1), sort: (r) => r.dist_pct, dica: 'Distância do strike ao preço' },
        { t: 'Equilíbrio', v: (r) => fmt.n2(r.equilibrio), sort: (r) => r.equilibrio },
        { t: 'Spread', v: (r) => fmt.pct(r.spread, 1), sort: (r) => r.spread },
        { t: 'Liq.', v: (r) => r.liq, sort: (r) => r.liq },
        { t: 'Suporte', v: (r) => (r.suporte ? pill('nota ' + fmt.n2(r.suporte.nota), 'ok') : pill('não', 'av')), cls: () => 'n', sort: (r) => (r.suporte ? r.suporte.nota : 0) },
        { t: 'Faixa', v: (r) => (r.fora_2s ? pill('fora de 2σ', 'ok') : r.fora_1s ? pill('fora de 1σ') : pill('dentro de 1σ', 'av')), cls: () => 'n' },
        { t: 'Estilo', v: (r) => r.estilo, cls: () => 'n' },
      ], o.puts.itens, {
        detalhe: (r) => h('div', null, h('div', { class: 'obs' }, r.obs),
          h('div', { class: 'obs', style: 'margin-top:6px' }, 'Faixa até o vencimento: 1σ ' + fmt.n2(r.faixa_1s[0]) + ' – ' + fmt.n2(r.faixa_1s[1]) + ' · 2σ ' + fmt.n2(r.faixa_2s[0]) + ' – ' + fmt.n2(r.faixa_2s[1]) + ' · negócios (10 pregões) ' + fmt.n0(r.neg10) + ' · vol. IV ' + fmt.pct((r.iv || 0) * 100, 0)),
          h('div', { class: 'acoes' }, h('button', { class: 'btn prim peq', onclick: (e) => { e.stopPropagation(); abrirGrafico(r.ticker, [
            { p: r.strike, label: 'Strike ' + r.serie, cor: 'down', forcar: true },
            { p: r.equilibrio, label: 'Equilíbrio', cor: 'warn', tracejado: true, forcar: true },
          ], r.dte); } }, 'Ver no gráfico'))),
      }));
    }
    const ex = Object.entries(o.puts.excluidos || {}).sort((a, b) => b[1] - a[1]);
    if (ex.length) sec.append(h('details', { class: 'exc' }, h('summary', null, 'Por que as outras séries ficaram de fora (' + fmt.n0(o.puts.total_series_put) + ' séries de PUT analisadas)'),
      h('ul', { class: 'simples' }, ex.map(([k, v]) => h('li', null, k + ': ' + fmt.n0(v))))));
  }

  /* ------------------------------------------------------------------ Entradas CALL */
  function montaCalls() {
    const sec = $('#sec-calls');
    sec.replaceChildren();
    const o = S.opt;
    if (!o) { sec.append(h('div', { class: 'vazio' }, 'Dados de opções ainda não gerados.')); return; }
    sec.append(h('div', { class: 'card' }, h('h2', null, 'Entradas em CALL nas zonas de interesse'),
      h('div', { class: 'muted' }, 'Ativos que estão numa zona de confluência. As séries sugeridas combinam liquidez (60%) e custo-benefício até o alvo 1 (40%). O valor estimado em cada alvo usa Black-Scholes com a volatilidade da própria série e supõe que o alvo é atingido em até 1/3 do prazo (máx. 20 dias).')));
    if (!o.entradas_call.length) { sec.append(h('div', { class: 'vazio' }, 'Nenhum ativo em zona de interesse agora.')); return; }
    for (const e of o.entradas_call) {
      const card = h('div', { class: 'entrada-card' },
        h('header', null, h('span', { class: 'tk' }, e.ticker + ' · ' + fmt.n2(e.preco)), h('span', { class: 'nota' }, 'nota ' + fmt.n2(e.nota))),
        h('div', { class: 'muted' }, 'Zona ' + fmt.n2(e.zona[0]) + ' – ' + fmt.n2(e.zona[1]) + ' · ' + e.fatores.join(' · ')),
        e.gatilhos.length ? h('div', { class: 'chips', style: 'margin-top:6px' }, e.gatilhos.map((g) => h('span', { class: 'chip info' }, g))) : null,
        h('div', { class: 'muted', style: 'margin-top:6px' }, e.alvos.length ? 'Alvos: ' + e.alvos.map((a, k) => (k + 1) + ') ' + fmt.n2(a.p) + ' (' + a.rot + ')').join(' · ') : 'Sem alvos acima do preço.'),
        h('div', { class: 'acoes linha', style: 'margin-top:8px' }, h('button', { class: 'btn prim peq', onclick: () => abrirGrafico(e.ticker) }, 'Ver no gráfico')));
      if (e.series.length) {
        card.append(tabela([
          { t: 'Série', v: (r) => r.serie, l: true },
          { t: 'Strike', v: (r) => fmt.n2(r.strike) },
          { t: 'Venc.', v: (r) => fmt.dm(r.venc) + ' · ' + r.dte + 'd' },
          { t: 'Pagar (venda)', v: (r) => fmt.n2(r.ask) },
          { t: 'Perda máx.', v: (r) => fmt.brl(r.perda_max_lote100) + '/100' },
          { t: 'Equilíbrio', v: (r) => fmt.n2(r.equilibrio) + ' (' + fmt.spct(r.mov_ate_equilibrio_pct) + ')' },
          { t: 'Delta', v: (r) => fmt.n2(r.delta) },
          { t: 'Tempo/dia', v: (r) => fmt.n2(r.theta_dia) },
          { t: 'Alvo 1', v: (r) => (r.alvos[0] ? fmt.n2(r.alvos[0].valor_est) + ' (' + fmt.spct(r.alvos[0].ganho_pct, 0) + ')' : '–') },
          { t: 'Alvo 2', v: (r) => (r.alvos[1] ? fmt.n2(r.alvos[1].valor_est) + ' (' + fmt.spct(r.alvos[1].ganho_pct, 0) + ')' : '–') },
          { t: 'Spread', v: (r) => fmt.pct(r.spread, 1) },
          { t: 'Liq.', v: (r) => r.liq },
        ], e.series, { detalhe: (r) => h('div', { class: 'obs' }, r.obs) }));
      } else {
        const ex = Object.entries(e.excluidas || {}).map(([k, v]) => k + ': ' + v).join(' · ');
        card.append(h('div', { class: 'aviso', style: 'margin-top:8px' }, 'Nenhuma série de CALL líquida passou pelos filtros. ' + (ex ? 'Motivos: ' + ex + '.' : '')));
      }
      sec.append(card);
    }
  }

  /* ------------------------------------------------------------------ Quedas e CALLs longas */
  function montaQuedas() {
    const sec = $('#sec-quedas');
    sec.replaceChildren();
    const o = S.opt;
    if (!o) { sec.append(h('div', { class: 'vazio' }, 'Dados de opções ainda não gerados.')); return; }
    sec.append(h('div', { class: 'card' }, h('h2', null, 'Ações que mais caíram e CALLs longas'),
      h('div', { class: 'muted' }, 'Ordenadas pela queda desde a máxima de 52 semanas, sem corte fixo. A CALL longa precisa ter 250 dias ou mais, estar perto do dinheiro e ter liquidez mínima. Fundamentos (dívida, lucro, caixa) ainda não são verificados: falta fonte gratuita confiável.')));
    sec.append(tabela([
      { t: 'Ativo', v: (r) => h('span', { class: 'tk' }, r.ticker), l: true, sort: (r) => r.ticker },
      { t: 'Preço', v: (r) => fmt.n2(r.preco) },
      { t: 'Queda 52 sem.', v: (r) => fmt.pct(r.da_max_52s_pct, 1), sort: (r) => -r.da_max_52s_pct, cls: () => 'n down' },
      { t: 'Estabilização', v: (r) => (r.estabilizacao.length ? pill(r.estabilizacao.length + ' sinal(is)', 'ok') : pill('nenhum', 'av')), cls: () => 'n', sort: (r) => r.estabilizacao.length },
      { t: 'Estrutura', v: (r) => r.estrutura, cls: () => 'n' },
      { t: 'Semanal', v: (r) => r.semanal, cls: () => 'n' },
      { t: 'CALL longa', v: (r) => (r.serie_longa ? r.serie_longa.serie + ' · ' + fmt.dm(r.serie_longa.venc) + ' · ' + r.serie_longa.dte + 'd' : pill('sem série líquida', 'av')), cls: () => 'n', l: true },
      { t: 'Strike', v: (r) => (r.serie_longa ? fmt.n2(r.serie_longa.strike) : '–') },
      { t: 'Pagar', v: (r) => (r.serie_longa ? fmt.n2(r.serie_longa.ask) : '–') },
      { t: 'Equilíbrio', v: (r) => (r.serie_longa ? fmt.n2(r.serie_longa.equilibrio) + ' (' + fmt.spct(r.serie_longa.mov_ate_equilibrio_pct) + ')' : '–') },
      { t: 'Liq.', v: (r) => (r.serie_longa ? r.serie_longa.liq : '–') },
    ], o.calls_longas, {
      detalhe: (r) => h('div', null, h('div', { class: 'obs' }, r.obs),
        r.estabilizacao.length ? h('div', { class: 'chips', style: 'margin:6px 0' }, r.estabilizacao.map((g) => h('span', { class: 'chip info' }, g))) : null,
        r.serie_longa ? h('div', { class: 'obs' }, 'No vencimento (resultado da CALL × resultado da ação): ' + r.serie_longa.cenarios.map((c) => fmt.spct(c.mov_pct, 0) + ' → CALL ' + fmt.spct(c.resultado_pct, 0) + ' / ação ' + fmt.spct(c.acao_pct, 0)).join(' · ') + '. Perda máxima: o prêmio inteiro (' + fmt.n2(r.serie_longa.perda_max) + ' por opção).') : null,
        h('div', { class: 'acoes' }, h('button', { class: 'btn prim peq', onclick: (e) => { e.stopPropagation(); abrirGrafico(r.ticker); } }, 'Ver no gráfico'))),
    }));
  }

  /* ------------------------------------------------------------------ Posições */
  function montaPosicoes() {
    const sec = $('#sec-posicoes');
    sec.replaceChildren();
    const o = S.opt;
    sec.append(h('div', { class: 'card' }, h('h2', null, 'Posições abertas'),
      h('div', { class: 'muted' }, 'Edite o arquivo positions.json no GitHub (veja o exemplo dentro dele). PUT vendida não tem regra de saída: o app só acompanha strike, equilíbrio, dias e risco de exercício. CALL comprada tem alvos e alertas.')));
    if (!o || !o.posicoes.length) { sec.append(h('div', { class: 'vazio' }, 'Nenhuma posição cadastrada.')); return; }
    sec.append(tabela([
      { t: 'Ativo', v: (r) => h('span', { class: 'tk' }, r.ticker), l: true },
      { t: 'Série', v: (r) => r.serie, l: true },
      { t: 'Lado', v: (r) => (r.tp ? r.lado + ' ' + r.tp : r.lado), cls: () => 'n', l: true },
      { t: 'Qtd', v: (r) => fmt.n0(r.qtd) },
      { t: 'Entrada', v: (r) => fmt.n2(r.preco_entrada) },
      { t: 'Atual', v: (r) => fmt.n2(r.preco_atual) },
      { t: 'Resultado', v: (r) => (r.resultado_rs !== undefined ? fmt.brl(r.resultado_rs) + ' (' + fmt.spct(r.resultado_pct, 0) + ')' : '–'), cls: (r) => 'n ' + (r.resultado_rs > 0 ? 'up' : r.resultado_rs < 0 ? 'down' : '') },
      { t: 'Dias', v: (r) => (r.dte !== undefined ? r.dte : '–') },
      { t: 'Alertas', v: (r) => (r.alertas.length ? pill(r.alertas.length + '', 'av') : pill('ok', 'ok')), cls: () => 'n' },
    ], o.posicoes, { detalhe: (r) => h('div', null, h('ul', { class: 'simples' }, r.alertas.map((a) => h('li', null, a))),
      h('div', { class: 'acoes' }, r.ticker ? h('button', { class: 'btn prim peq', onclick: (e) => { e.stopPropagation(); abrirGrafico(r.ticker, [
        r.preco_entrada ? { p: r.strike, label: 'Strike', cor: 'accent', forcar: true } : null,
        r.equilibrio ? { p: r.equilibrio, label: 'Equilíbrio', cor: 'warn', tracejado: true, forcar: true } : null,
      ].filter(Boolean)); } }, 'Ver no gráfico') : null)) }));
  }

  /* ------------------------------------------------------------------ Sobre */
  function montaSobre() {
    const sec = $('#sec-sobre');
    sec.replaceChildren();
    const avisos = (S.meta && S.meta.avisos) || [];
    const prem = (S.opt && S.opt.premissas) || [];
    sec.append(
      h('div', { class: 'card' }, h('h2', null, 'Como ler este app'),
        h('ul', { class: 'simples' },
          h('li', null, 'Nota da zona = soma dos fatores que coincidem (Fibonacci, médias, FVG, order block, liquidez, desconto, semanal, volume). Mais fatores, nota maior.'),
          h('li', null, 'Faixa até o vencimento = preço × volatilidade histórica × √(dias/365); 1σ cobre cerca de 68% dos desfechos, 2σ cerca de 95%. Quedas bruscas passam dessa faixa com mais frequência.'),
          h('li', null, 'Chance de exercício vem do modelo de Black-Scholes com a volatilidade implícita da série; é uma aproximação.'),
          h('li', null, 'Os alertas só aparecem quando você abre o app: não há notificação.'))),
      avisos.length ? h('div', { class: 'card' }, h('h2', null, 'Avisos'), h('ul', { class: 'simples' }, avisos.map((a) => h('li', null, a)))) : null,
      prem.length ? h('div', { class: 'card' }, h('h2', null, 'Premissas dos cálculos de opções'), h('ul', { class: 'simples' }, prem.map((a) => h('li', null, a))),
        S.opt && S.opt.selic ? h('div', { class: 'muted', style: 'margin-top:8px' }, 'Selic usada: ' + fmt.pct(S.opt.selic.aa, 2) + ' a.a. (' + S.opt.selic.fonte + ')') : null) : null,
      S.uni ? h('div', { class: 'card' }, h('h2', null, 'Universo (' + S.uni.itens.length + ' ativos)'), h('div', { class: 'muted' }, S.uni.criterio + '. Calculado em ' + fmt.horaBR(S.uni.calculado_em) + '.'),
        h('div', { class: 'chips', style: 'margin-top:8px' }, S.uni.itens.map((i) => h('span', { class: 'chip info' }, i.rank + '. ' + i.ticker)))) : null);
  }

  /* ------------------------------------------------------------------ início */
  async function iniciar() {
    const nav = $('#abas');
    for (const [id, nome] of ABAS) {
      nav.append(h('button', { class: 'aba', id: 'aba-' + id, role: 'tab', 'aria-selected': 'false', onclick: () => mostraAba(id) }, nome));
      $('#conteudo').append(h('section', { id: 'sec-' + id, class: 'hidden', role: 'tabpanel' }));
    }
    try {
      S.meta = await getJSON('data/meta.json');
      S.v = S.meta.gerado_em;
    } catch (e) {
      pintaStatus();
      $('#avisos').append(h('div', { class: 'vazio' }, 'Os dados ainda não foram gerados. No GitHub, abra a aba Actions, escolha "Atualizar dados" e clique em Run workflow. Depois recarregue esta página.'));
      mostraAba('sobre');
      return;
    }
    const pedidos = await Promise.allSettled([getJSON('data/universo.json'), getJSON('data/alertas.json'), getJSON('data/quedas.json'), getJSON('data/options.json')]);
    [S.uni, S.alertas, S.quedas, S.opt] = pedidos.map((p) => (p.status === 'fulfilled' ? p.value : null));
    pintaStatus();
    pintaAlertas();
    montaGrafico();
    montaPuts(); montaCalls(); montaQuedas(); montaPosicoes(); montaSobre();
    const inicio = (location.hash || '#grafico').slice(1);
    mostraAba(ABAS.some(([a]) => a === inicio) ? inicio : 'grafico');
    const t0 = S.alertas && S.alertas.itens[0] ? S.alertas.itens[0].ticker : S.uni && S.uni.itens[0] ? S.uni.itens[0].ticker : null;
    if (t0) { $('#sel-ativo').value = t0; await carregaAtivo(t0); }
  }

  document.addEventListener('DOMContentLoaded', iniciar);
})();
