#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_data.py  -  preços, universo de liquidez, indicadores, SMC e zonas de confluência.

Fonte (gratuita e oficial): arquivo COTAHIST da B3 (cotações históricas de fim de dia).
  - COTAHIST_A<ano>.ZIP  (anual)   e, se faltarem dias, COTAHIST_D<ddmmaaaa>.ZIP (diário)
  - Se você já baixou os ZIPs, coloque-os numa pasta e use  --dir  (ou a variável COTAHIST_DIR).

Saídas (pasta data/):
  meta.json, universo.json, alertas.json, quedas.json, ativos/<TICKER>.json
Cache de trabalho (pasta .cache/, não vai para o site):
  options_rows.json, universo.json   -> lidos por build_options.py

Todas as regras do SMC e os limiares estão em config.json (ou em DEFAULT_CONFIG abaixo).
Este script apoia a decisão; não é recomendação de investimento.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import io
import json
import math
import os
import sys
import time
import zipfile
from collections import defaultdict

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
ATIVOS_DIR = os.path.join(DATA_DIR, "ativos")
CACHE_DIR = os.path.join(ROOT, ".cache")
BRT = dt.timezone(dt.timedelta(hours=-3))

DEFAULT_CONFIG = {
    "fontes": {
        "base_urls": ["https://bvmf.bmfbovespa.com.br/InstDados/SerieHistorica/"],
        "anos_historico": 2,
        "dias_diarios_max": 10,
    },
    "universo": {"tamanho": 25, "janela_pregoes": 20, "min_presenca": 0.8, "candidatos": 60},
    "grafico": {"candles_diarios": 260, "candles_semanais": 130},
    "smc": {
        "swing_n": 3,                # candles de cada lado para confirmar swing high/low
        "fvg_min_pct": 0.30,         # tamanho mínimo do vão, em % do preço
        "ob_usar_pavios": True,      # order block = candle inteiro (True) ou só o corpo (False)
        "tol_liquidez_pct": 0.15,    # tolerância para topos/fundos iguais
        "janela_perna": 90,          # candles para achar a perna (topo e fundo) do Fibonacci
        "olhar_fundo_antes": 60,
    },
    "indicadores": {"mm": [21, 50, 200], "rsi": 14, "macd": [12, 26, 9], "atr": 14, "mm_semanal": 20},
    "zonas": {
        "tolerancia_pct": 1.2,       # distância para juntar fatores numa mesma zona
        "largura_max_pct": 3.5,
        "nota_minima_alerta": 4.0,
        "proximidade_pct": 1.5,      # quão acima do topo da zona o preço ainda conta como "na zona"
        "volume_reacao_x": 1.3,      # volume do último candle / média de 20
        "pesos": {
            "fib382": 1.0, "fib500": 1.5, "fib618": 1.5, "fib786": 1.0,
            "mm21": 1.0, "mm50": 1.0, "mm200": 1.5,
            "fvg": 1.5, "ob": 2.0, "liq": 1.0, "liq_igual": 1.5,
            "semanal": 1.5, "desconto": 1.0, "volume": 1.0,
        },
    },
}

AVISOS = [
    "Dados de fim de dia (COTAHIST da B3), sem ajuste por dividendos, desdobramentos ou bonificações: "
    "saltos de preço por eventos corporativos podem distorcer indicadores e marcações.",
    "O app estima zonas e faixas de preço; não prevê preço nem é recomendação de investimento.",
]


# --------------------------------------------------------------------------- utilidades
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def deep_merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            deep_merge(cfg, json.load(fh))
    return cfg


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))


def r2(x):
    return None if x is None else round(float(x), 2)


def r4(x):
    return None if x is None else round(float(x), 4)


# --------------------------------------------------------------------------- download e leitura do COTAHIST
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; opcoes-app/1.0)"}


def http_get(url, tries=3, timeout=180):
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(2 * (k + 1))
    log(f"   não foi possível baixar {url} ({last})")
    return None


def get_zip(name, cfg, local_dir):
    """Procura o ZIP numa pasta local (se informada) e, senão, baixa."""
    if local_dir and os.path.isdir(local_dir):
        for f in os.listdir(local_dir):
            if f.upper() == name.upper():
                with open(os.path.join(local_dir, f), "rb") as fh:
                    log(f"   usando arquivo local {f}")
                    return fh.read()
    for base in cfg["fontes"]["base_urls"]:
        data = http_get(base + name)
        if data:
            log(f"   baixado {name} ({len(data) / 1e6:.1f} MB)")
            return data
    return None


def iter_lines(zbytes):
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        for member in zf.namelist():
            with zf.open(member) as fh:
                for raw in io.TextIOWrapper(fh, encoding="latin-1", newline=None):
                    if raw[:2] == "01":
                        yield raw


def money(s):
    return int(s) / 100.0


def parse_stock(line):
    fat = int(line[210:217]) or 1
    return {
        "data": f"{line[2:6]}-{line[6:8]}-{line[8:10]}",
        "cod": line[12:24].strip(),
        "nome": line[27:39].strip(),
        "o": money(line[56:69]) / fat, "h": money(line[69:82]) / fat,
        "l": money(line[82:95]) / fat, "c": money(line[108:121]) / fat,
        "neg": int(line[147:152]), "vol": money(line[170:188]),
    }


def parse_option(line):
    fat = int(line[210:217]) or 1
    venc = line[202:210]
    return {
        "data": f"{line[2:6]}-{line[6:8]}-{line[8:10]}",
        "cod": line[12:24].strip(),
        "tp": "C" if line[24:27] == "070" else "P",
        "ult": money(line[108:121]) / fat,
        "ofc": money(line[121:134]) / fat,
        "ofv": money(line[134:147]) / fat,
        "neg": int(line[147:152]),
        "vol": money(line[170:188]),
        "exe": money(line[188:201]) / fat,
        "ven": f"{venc[0:4]}-{venc[4:6]}-{venc[6:8]}",
    }


def business_days_between(d0, d1):
    d = d0 + dt.timedelta(days=1)
    while d <= d1:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def load_sources(cfg, local_dir):
    """Devolve a lista de ZIPs (bytes) a ler, do mais antigo para o mais novo."""
    today = dt.datetime.now(BRT).date()
    zips = []
    for ano in range(today.year - cfg["fontes"]["anos_historico"] + 1, today.year + 1):
        z = get_zip(f"COTAHIST_A{ano}.ZIP", cfg, local_dir)
        if z:
            zips.append(z)
        elif ano == today.year:
            log(f"   AVISO: arquivo anual de {ano} indisponível")
    if not zips:
        return zips, None
    # último pregão presente no anual mais recente
    last = None
    for line in iter_lines(zips[-1]):
        if line[10:12] == "02" and line[24:27] == "010":
            d = line[2:10]
            if last is None or d > last:
                last = d
    if last:
        last_d = dt.date(int(last[:4]), int(last[4:6]), int(last[6:8]))
        extra = 0
        for d in business_days_between(last_d, today):
            if extra >= cfg["fontes"]["dias_diarios_max"]:
                break
            z = get_zip(f"COTAHIST_D{d:%d%m%Y}.ZIP", cfg, local_dir)
            if z:
                zips.append(z)
                extra += 1
    return zips, last


# --------------------------------------------------------------------------- indicadores
def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_wilder(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd_calc(closes, f=12, s=26, sig=9):
    ef, es = ema(closes, f), ema(closes, s)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    valid = [(i, v) for i, v in enumerate(line) if v is not None]
    signal = [None] * len(closes)
    if len(valid) >= sig:
        idx0 = valid[0][0]
        sub = ema([v for _, v in valid], sig)
        for j, v in enumerate(sub):
            signal[idx0 + j] = v
    hist = [None if (a is None or b is None) else a - b for a, b in zip(line, signal)]
    return line, signal, hist


def atr_calc(h, l, c, n=14):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return sma(tr, n)


def hist_vol(closes, n):
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


# --------------------------------------------------------------------------- SMC
def find_swings(h, l, n):
    sw = []
    for i in range(n, len(h) - n):
        if h[i] > max(h[i - n:i]) and h[i] >= max(h[i + 1:i + n + 1]):
            sw.append({"i": i, "k": "H", "p": h[i]})
        if l[i] < min(l[i - n:i]) and l[i] <= min(l[i + 1:i + n + 1]):
            sw.append({"i": i, "k": "L", "p": l[i]})
    sw.sort(key=lambda s: (s["i"], s["k"]))
    return sw


def structure_events(c, swings, n):
    """BOS / CHoCH por fechamento além do último swing confirmado (confirmação n candles depois)."""
    events, trend = [], 0
    last_h = last_l = None
    si = 0
    for i in range(len(c)):
        while si < len(swings) and swings[si]["i"] + n <= i:
            s = swings[si]
            ref = {"i": s["i"], "p": s["p"], "quebrado": False}
            if s["k"] == "H":
                last_h = ref
            else:
                last_l = ref
            si += 1
        if last_h and not last_h["quebrado"] and c[i] > last_h["p"]:
            events.append({"i": i, "tipo": "CHoCH" if trend == -1 else "BOS", "dir": "alta",
                           "nivel": last_h["p"], "origem": last_h["i"]})
            last_h["quebrado"] = True
            trend = 1
        elif last_l and not last_l["quebrado"] and c[i] < last_l["p"]:
            events.append({"i": i, "tipo": "CHoCH" if trend == 1 else "BOS", "dir": "baixa",
                           "nivel": last_l["p"], "origem": last_l["i"]})
            last_l["quebrado"] = True
            trend = -1
    return events, trend


def find_fvgs(o, h, l, c, min_pct):
    out = []
    n = len(c)
    for i in range(2, n):
        price = c[i - 1]
        if l[i] > h[i - 2] and (l[i] - h[i - 2]) / price * 100 >= min_pct:
            lo, hi, d = h[i - 2], l[i], "alta"
        elif h[i] < l[i - 2] and (l[i - 2] - h[i]) / price * 100 >= min_pct:
            lo, hi, d = h[i], l[i - 2], "baixa"
        else:
            continue
        status = "aberto"
        for j in range(i + 1, n):
            if d == "alta":
                if l[j] <= lo:
                    status = "preenchido"
                    break
                if l[j] < hi:
                    status = "parcial"
            else:
                if h[j] >= hi:
                    status = "preenchido"
                    break
                if h[j] > lo:
                    status = "parcial"
        if status != "preenchido":
            out.append({"i": i - 1, "lo": lo, "hi": hi, "dir": d, "status": status})
    return out


def find_order_blocks(o, h, l, c, events, use_wicks):
    out = []
    n = len(c)
    for ev in events:
        k, org = ev["i"], ev["origem"]
        if ev["dir"] == "alta":
            seg = range(org, k + 1)
            m = min(seg, key=lambda j: l[j])
            cand = next((j for j in range(m, max(m - 6, -1), -1) if c[j] < o[j]), None)
            if cand is None:
                continue
            lo = l[cand] if use_wicks else min(o[cand], c[cand])
            hi = h[cand] if use_wicks else max(o[cand], c[cand])
            mitigated = any(c[j] < lo for j in range(k + 1, n))
            if not mitigated:
                out.append({"i": cand, "lo": lo, "hi": hi, "dir": "alta"})
        else:
            seg = range(org, k + 1)
            m = max(seg, key=lambda j: h[j])
            cand = next((j for j in range(m, max(m - 6, -1), -1) if c[j] > o[j]), None)
            if cand is None:
                continue
            lo = l[cand] if use_wicks else min(o[cand], c[cand])
            hi = h[cand] if use_wicks else max(o[cand], c[cand])
            mitigated = any(c[j] > hi for j in range(k + 1, n))
            if not mitigated:
                out.append({"i": cand, "lo": lo, "hi": hi, "dir": "baixa"})
    return out


def liquidity_levels(h, l, swings, tol_pct):
    """Swings ainda não varridos (por pavio) e topos/fundos iguais."""
    n = len(h)
    pend = []
    for s in swings:
        if s["k"] == "H":
            swept = any(h[j] > s["p"] for j in range(s["i"] + 1, n))
        else:
            swept = any(l[j] < s["p"] for j in range(s["i"] + 1, n))
        if not swept:
            pend.append(dict(s))
    for a in pend:
        a["igual"] = any(
            b is not a and b["k"] == a["k"] and abs(b["p"] - a["p"]) / a["p"] * 100 <= tol_pct for b in pend
        )
    return pend


# --------------------------------------------------------------------------- zonas de confluência
def build_zones(cands, tol_pct, max_width_pct):
    items = sorted(cands, key=lambda x: x["lo"])
    zones = []
    for it in items:
        z = zones[-1] if zones else None
        if z and it["lo"] <= z["hi"] * (1 + tol_pct / 100) and it["hi"] <= z["lo"] * (1 + max_width_pct / 100):
            z["hi"] = max(z["hi"], it["hi"])
            z["itens"].append(it)
        else:
            zones.append({"lo": it["lo"], "hi": it["hi"], "itens": [it]})
    return zones


def analyze(ticker, nome, candles, cfg):
    """candles: lista de dicts ordenada por data. Devolve o dicionário exportado para o site."""
    sm, ind, zc = cfg["smc"], cfg["indicadores"], cfg["zonas"]
    dates = [x["data"] for x in candles]
    o = [x["o"] for x in candles]
    h = [x["h"] for x in candles]
    l = [x["l"] for x in candles]
    c = [x["c"] for x in candles]
    v = [x["vol"] for x in candles]
    n = len(c)
    price = c[-1]

    mms = {p: sma(c, p) for p in ind["mm"]}
    rsi = rsi_wilder(c, ind["rsi"])
    ml, ms, mh = macd_calc(c, *ind["macd"])
    atr = atr_calc(h, l, c, ind["atr"])
    vol20 = sma(v, 20)

    swings = find_swings(h, l, sm["swing_n"])
    events, trend = structure_events(c, swings, sm["swing_n"])
    fvgs = find_fvgs(o, h, l, c, sm["fvg_min_pct"])
    obs = find_order_blocks(o, h, l, c, events, sm["ob_usar_pavios"])
    liq = liquidity_levels(h, l, swings, sm["tol_liquidez_pct"])

    # --- semanal
    weeks = {}
    for i, d in enumerate(dates):
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        weeks.setdefault((y, w), []).append(i)
    wk = []
    for idxs in weeks.values():
        wk.append({"d": dates[idxs[-1]], "o": o[idxs[0]], "h": max(h[i] for i in idxs),
                   "l": min(l[i] for i in idxs), "c": c[idxs[-1]], "v": sum(v[i] for i in idxs)})
    wk.sort(key=lambda x: x["d"])
    wc = [x["c"] for x in wk]
    wma = sma(wc, ind["mm_semanal"])
    w_trend = "indefinida"
    if wma[-1] is not None and len(wk) > ind["mm_semanal"] + 4 and wma[-5] is not None:
        if wc[-1] > wma[-1] and wma[-1] > wma[-5]:
            w_trend = "alta"
        elif wc[-1] < wma[-1] and wma[-1] < wma[-5]:
            w_trend = "baixa"
        else:
            w_trend = "lateral"

    # --- perna e Fibonacci (topo recente e fundo anterior)
    W = min(sm["janela_perna"], n)
    iH = max(range(n - W, n), key=lambda j: h[j])
    lo_start = max(0, iH - sm["olhar_fundo_antes"])
    iL = min(range(lo_start, iH + 1), key=lambda j: l[j])
    H, L = h[iH], l[iL]
    fib = None
    retr = 0.0
    if iH > iL and H > L:
        retr = max(0.0, (H - price) / (H - L) * 100)
        fib = {
            "iH": iH, "iL": iL, "H": H, "L": L, "retr_pct": retr,
            "niveis": [{"r": r, "p": H - (H - L) * r} for r in (0.382, 0.5, 0.618, 0.786)],
            "ext": [{"r": r, "p": L + (H - L) * r} for r in (1.272, 1.618)],
        }
    eq_lo = min(l[max(0, n - 120):])
    eq_hi = max(h[max(0, n - 120):])
    eq = (eq_lo + eq_hi) / 2
    em_retracao = bool(fib) and retr >= 15 and trend >= 0 and w_trend != "baixa"

    # --- candidatos a zonas de suporte
    W_ = zc["pesos"]
    cands = []
    if fib and iH > iL:
        for nv in fib["niveis"]:
            key = {0.382: "fib382", 0.5: "fib500", 0.618: "fib618", 0.786: "fib786"}[nv["r"]]
            cands.append({"lo": nv["p"], "hi": nv["p"], "tipo": key, "peso": W_[key],
                          "rot": f"Fibonacci {nv['r'] * 100:.1f}%".replace(".", ",")})
    for p in ind["mm"]:
        val = mms[p][-1]
        if val is not None:
            key = f"mm{p}"
            cands.append({"lo": val, "hi": val, "tipo": key, "peso": W_.get(key, 1.0), "rot": f"MM{p}"})
    if wma[-1] is not None:
        cands.append({"lo": wma[-1], "hi": wma[-1], "tipo": "semanal", "peso": W_["semanal"],
                      "rot": f"MM{ind['mm_semanal']} semanal"})
    for f in fvgs:
        if f["dir"] == "alta":
            cands.append({"lo": f["lo"], "hi": f["hi"], "tipo": "fvg", "peso": W_["fvg"],
                          "rot": "FVG de alta " + ("parcial" if f["status"] == "parcial" else "aberto")})
    for b in obs:
        if b["dir"] == "alta":
            cands.append({"lo": b["lo"], "hi": b["hi"], "tipo": "ob", "peso": W_["ob"], "rot": "Order block de alta"})
    for s in liq:
        if s["k"] == "L":
            cands.append({"lo": s["p"], "hi": s["p"], "tipo": "liq_igual" if s["igual"] else "liq",
                          "peso": W_["liq_igual"] if s["igual"] else W_["liq"],
                          "rot": "Fundos iguais (liquidez)" if s["igual"] else "Fundo anterior (liquidez)"})

    zones = build_zones(cands, zc["tolerancia_pct"], zc["largura_max_pct"])
    ultimo_vol_ok = vol20[-1] is not None and v[-1] > zc["volume_reacao_x"] * vol20[-1]
    zonas_out = []
    for z in zones:
        por_tipo = {}
        for it in z["itens"]:
            if it["tipo"] not in por_tipo or it["peso"] > por_tipo[it["tipo"]]["peso"]:
                por_tipo[it["tipo"]] = it
        nota = sum(x["peso"] for x in por_tipo.values())
        fatores = [x["rot"] for x in por_tipo.values()]
        if z["hi"] <= eq:
            nota += W_["desconto"]
            fatores.append("Zona de desconto")
        toque = min(l[-3:]) <= z["hi"] * 1.005 and max(h[-3:]) >= z["lo"] * 0.995
        if toque and ultimo_vol_ok:
            nota += W_["volume"]
            fatores.append("Volume acima da média na reação")
        mid = (z["lo"] + z["hi"]) / 2
        na_zona = z["lo"] * 0.995 <= price <= z["hi"] * (1 + zc["proximidade_pct"] / 100)
        zonas_out.append({"lo": z["lo"], "hi": z["hi"], "nota": nota, "fatores": fatores,
                          "dist_pct": (price - mid) / price * 100, "na_zona": na_zona})
    zonas_out.sort(key=lambda z: -z["nota"])

    # --- gatilhos de confirmação
    gat = []
    if rsi[-2] is not None and rsi[-1] is not None and rsi[-2] < 30 <= rsi[-1]:
        gat.append("RSI saindo de sobrevenda")
    for k in range(1, 4):
        if len(ml) > k + 1 and ml[-k] is not None and ms[-k] is not None and ml[-k - 1] is not None and ms[-k - 1] is not None:
            if ml[-k - 1] <= ms[-k - 1] and ml[-k] > ms[-k]:
                gat.append("MACD cruzou para cima")
                break
    rec = [e for e in events if e["i"] >= n - 10]
    if any(e["dir"] == "alta" and e["tipo"] == "CHoCH" for e in rec):
        gat.append("CHoCH de alta recente")
    if any(e["dir"] == "baixa" and e["tipo"] == "CHoCH" for e in rec):
        gat.append("CHoCH de baixa recente")
    na = [z for z in zonas_out if z["na_zona"] and z["hi"] <= price * 1.02]
    if na and c[-1] > o[-1] and l[-1] <= na[0]["hi"] * 1.005:
        gat.append("Candle de alta reagindo na zona")
    if ultimo_vol_ok:
        gat.append("Volume acima da média")

    # --- alvos para CALLs (acima do preço)
    acima = []
    for s in liq:
        if s["k"] == "H" and s["p"] > price:
            acima.append({"p": s["p"], "rot": "Topo anterior (liquidez)"})
    for f in fvgs:
        if f["dir"] == "baixa" and f["lo"] > price:
            acima.append({"p": f["lo"], "rot": "FVG de baixa acima"})
    if fib and H > price:
        acima.append({"p": H, "rot": "Último topo (início da retração)"})
    if fib:
        acima.append({"p": fib["ext"][1]["p"], "rot": "Extensão de Fibonacci 161,8%"})
    acima.sort(key=lambda x: x["p"])
    alvos = []
    for a in acima:
        if not alvos:
            if a["p"] >= price * 1.02:
                alvos.append(a)
        elif a["p"] >= alvos[-1]["p"] * 1.03:
            alvos.append(a)
        if len(alvos) == 2:
            break

    # --- exportação (janela do gráfico)
    N = min(cfg["grafico"]["candles_diarios"], n)
    off = n - N

    def tail(arr, nd=4):
        return [None if x is None else round(x, nd) for x in arr[off:]]

    def ix(i):
        return max(0, i - off)

    hv20, hv60 = hist_vol(c, 20), hist_vol(c, 60)
    pico52 = max(h[max(0, n - 252):])
    ult = candles[-1]
    prev = c[-2] if n > 1 else c[-1]
    out = {
        "ticker": ticker, "nome": nome,
        "ultimo": {"data": ult["data"], "close": r2(price), "var_dia_pct": r2((price / prev - 1) * 100)},
        "candles": [[dates[i], r2(o[i]), r2(h[i]), r2(l[i]), r2(c[i]), round(v[i])] for i in range(off, n)],
        "semanal": [[x["d"], r2(x["o"]), r2(x["h"]), r2(x["l"]), r2(x["c"]), round(x["v"])]
                    for x in wk[-cfg["grafico"]["candles_semanais"]:]],
        "ind": {
            **{f"mm{p}": tail(mms[p], 3) for p in ind["mm"]},
            "rsi": tail(rsi, 2), "macd": tail(ml, 4), "sinal": tail(ms, 4), "hist": tail(mh, 4),
        },
        "smc": {
            "swings": [{"i": s["i"] - off, "k": s["k"], "p": r2(s["p"])} for s in swings if s["i"] >= off],
            "eventos": [{"i": e["i"] - off, "tipo": e["tipo"], "dir": e["dir"], "nivel": r2(e["nivel"]),
                         "origem": ix(e["origem"])} for e in events if e["i"] >= off],
            "fvg": [{"i": ix(f["i"]), "lo": r2(f["lo"]), "hi": r2(f["hi"]), "dir": f["dir"], "status": f["status"]}
                    for f in fvgs],
            "ob": [{"i": ix(b["i"]), "lo": r2(b["lo"]), "hi": r2(b["hi"]), "dir": b["dir"]} for b in obs],
            "liquidez": [{"i": ix(s["i"]), "k": s["k"], "p": r2(s["p"]), "igual": s["igual"]} for s in liq],
            "eq": r2(eq), "range": [r2(eq_lo), r2(eq_hi)],
            "tendencia": {1: "alta", -1: "baixa", 0: "indefinida"}[trend],
        },
        "fib": None if not fib else {
            "iH": fib["iH"] - off, "iL": ix(fib["iL"]), "H": r2(fib["H"]), "L": r2(fib["L"]),
            "retr_pct": r2(fib["retr_pct"]),
            "niveis": [{"r": x["r"], "p": r2(x["p"])} for x in fib["niveis"]],
            "ext": [{"r": x["r"], "p": r2(x["p"])} for x in fib["ext"]],
        },
        "zonas": [{"lo": r2(z["lo"]), "hi": r2(z["hi"]), "nota": round(z["nota"], 1), "fatores": z["fatores"],
                   "dist_pct": r2(z["dist_pct"]), "na_zona": z["na_zona"]} for z in zonas_out[:12]],
        "alvos": [{"p": r2(a["p"]), "rot": a["rot"]} for a in alvos],
        "gatilhos": gat,
        "contexto": {
            "em_retracao": em_retracao, "retr_pct": r2(retr), "semanal": w_trend,
            "estrutura": {1: "alta", -1: "baixa", 0: "indefinida"}[trend],
            "rsi": r2(rsi[-1]), "atr_pct": r2((atr[-1] / price * 100) if atr[-1] else None),
        },
        "vol": {"hist20": r4(hv20), "hist60": r4(hv60)},
        "queda": {"da_max_52s_pct": r2((price / pico52 - 1) * 100), "max_52s": r2(pico52)},
    }
    return out


# --------------------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("COTAHIST_DIR"), help="pasta com COTAHIST_*.ZIP já baixados")
    args = ap.parse_args()
    cfg = load_config()
    os.makedirs(ATIVOS_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    log("1/5 Lendo fontes da B3...")
    zips, last_annual = load_sources(cfg, args.dir)
    if not zips:
        log("ERRO: nenhum arquivo COTAHIST disponível. Baixe os ZIPs no site da B3 (Séries Históricas) "
            "e rode com  --dir <pasta>  ou defina COTAHIST_DIR.")
        sys.exit(2)

    log("2/5 Lendo ações (mercado à vista, lote padrão)...")
    stocks = defaultdict(dict)
    names = {}
    for z in zips:
        for line in iter_lines(z):
            if line[10:12] == "02" and line[24:27] == "010":
                r = parse_stock(line)
                if r["c"] > 0:
                    stocks[r["cod"]][r["data"]] = r
                    names[r["cod"]] = r["nome"]
    sessions = sorted({d for s in stocks.values() for d in s})
    if len(sessions) < 60:
        log("ERRO: poucos pregões carregados.")
        sys.exit(2)
    last_date = sessions[-1]
    wnd = cfg["universo"]["janela_pregoes"]
    win = sessions[-wnd:]
    log(f"   {len(stocks)} papéis, {len(sessions)} pregões, último pregão {last_date}")

    # ranking por número médio de negócios
    ranked = []
    for cod, rows in stocks.items():
        pres = [rows[d] for d in win if d in rows]
        if len(pres) < cfg["universo"]["min_presenca"] * wnd or rows.get(last_date) is None:
            continue
        ranked.append({"ticker": cod, "nome": names[cod], "neg_med": sum(x["neg"] for x in pres) / wnd,
                       "vol_med": sum(x["vol"] for x in pres) / wnd})
    ranked.sort(key=lambda x: (-x["neg_med"], -x["vol_med"]))
    seen, cands = set(), []
    for r in ranked:
        if r["ticker"][:4] in seen:
            continue
        seen.add(r["ticker"][:4])
        cands.append(r)
        if len(cands) >= cfg["universo"]["candidatos"]:
            break
    prefix_to_ticker = {r["ticker"][:4]: r["ticker"] for r in cands}

    log("3/5 Lendo opções dos candidatos (últimos pregões)...")
    win_set = set(win)
    opt_rows, opt_trades = [], defaultdict(int)
    for z in zips:
        for line in iter_lines(z):
            tp = line[24:27]
            if tp not in ("070", "080"):
                continue
            pref = line[12:16]
            if pref not in prefix_to_ticker:
                continue
            d = f"{line[2:6]}-{line[6:8]}-{line[8:10]}"
            if d not in win_set:
                continue
            r = parse_option(line)
            opt_rows.append(r)
            opt_trades[pref] += r["neg"]
    # dedupe (mesmo pregão em anual e diário)
    uniq = {}
    for r in opt_rows:
        uniq[(r["data"], r["cod"])] = r
    opt_rows = list(uniq.values())

    universe = []
    for r in cands:
        if opt_trades.get(r["ticker"][:4], 0) > 0:
            universe.append(r)
        if len(universe) >= cfg["universo"]["tamanho"]:
            break
    log(f"   universo final: {len(universe)} tickers")

    log("4/5 Calculando indicadores, SMC e zonas...")
    alertas, quedas, uni_out = [], [], []
    keep_prefix = {r["ticker"][:4] for r in universe}
    for rank, r in enumerate(universe, 1):
        t = r["ticker"]
        rows = [stocks[t][d] for d in sorted(stocks[t])]
        if len(rows) < 80:
            log(f"   {t}: histórico curto, ignorado")
            continue
        res = analyze(t, r["nome"], rows, cfg)
        res["rank_liquidez"] = rank
        res["liquidez"] = {"neg_med": round(r["neg_med"]), "vol_med": round(r["vol_med"])}
        dump(os.path.join(ATIVOS_DIR, f"{t}.json"), res)
        uni_out.append({"ticker": t, "nome": r["nome"], "rank": rank, "neg_med": round(r["neg_med"]),
                        "vol_med": round(r["vol_med"]), "close": res["ultimo"]["close"],
                        "var_dia_pct": res["ultimo"]["var_dia_pct"], "tendencia": res["smc"]["tendencia"],
                        "semanal": res["contexto"]["semanal"], "em_retracao": res["contexto"]["em_retracao"]})
        zc = cfg["zonas"]
        melhores = [z for z in res["zonas"] if z["na_zona"] and z["nota"] >= zc["nota_minima_alerta"]]
        contexto_ok = res["contexto"]["estrutura"] != "baixa" or res["contexto"]["em_retracao"]
        if melhores and contexto_ok:
            z = melhores[0]
            alertas.append({"ticker": t, "preco": res["ultimo"]["close"], "zona": [z["lo"], z["hi"]],
                            "nota": z["nota"], "fatores": z["fatores"], "dist_pct": z["dist_pct"],
                            "gatilhos": res["gatilhos"], "contexto": res["contexto"],
                            "alvos": res["alvos"]})
        sinais = [g for g in res["gatilhos"] if g in ("RSI saindo de sobrevenda", "CHoCH de alta recente",
                                                      "Candle de alta reagindo na zona", "MACD cruzou para cima")]
        quedas.append({"ticker": t, "nome": r["nome"], "preco": res["ultimo"]["close"],
                       "da_max_52s_pct": res["queda"]["da_max_52s_pct"], "max_52s": res["queda"]["max_52s"],
                       "estabilizacao": sinais, "estrutura": res["contexto"]["estrutura"],
                       "semanal": res["contexto"]["semanal"], "rsi": res["contexto"]["rsi"]})
        log(f"   {t}: ok")
    alertas.sort(key=lambda a: -a["nota"])
    quedas.sort(key=lambda q: q["da_max_52s_pct"])

    log("5/5 Gravando arquivos...")
    now = dt.datetime.now(BRT)
    dump(os.path.join(DATA_DIR, "universo.json"),
         {"calculado_em": now.isoformat(timespec="minutes"), "criterio": "número médio de negócios (média de "
          f"{wnd} pregões), volume financeiro como desempate; só tickers com opções negociadas no período",
          "itens": uni_out})
    dump(os.path.join(DATA_DIR, "alertas.json"), {"ultimo_pregao": last_date, "itens": alertas})
    dump(os.path.join(DATA_DIR, "quedas.json"),
         {"ultimo_pregao": last_date, "fundamentos": "não implementado (sem fonte gratuita confiável)",
          "itens": quedas})
    dump(os.path.join(DATA_DIR, "meta.json"),
         {"gerado_em": now.isoformat(timespec="minutes"), "ultimo_pregao": last_date,
          "fonte": "B3 - COTAHIST (fim de dia)", "tickers": len(uni_out), "avisos": AVISOS,
          "config": {"zonas": cfg["zonas"]["nota_minima_alerta"], "swing_n": cfg["smc"]["swing_n"]}})
    dump(os.path.join(CACHE_DIR, "options_rows.json"),
         [r for r in opt_rows if r["cod"][:4] in keep_prefix])
    dump(os.path.join(CACHE_DIR, "universo.json"),
         {"ultimo_pregao": last_date, "prefixos": {t["ticker"][:4]: t["ticker"] for t in universe}})
    log(f"Concluído: {len(uni_out)} ativos, {len(alertas)} alertas.")


if __name__ == "__main__":
    main()
