#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_options.py  -  opções: liquidez, volatilidade implícita, gregas, rankings e posições.

Roda DEPOIS do build_data.py (usa .cache/options_rows.json e data/ativos/*.json).
Fonte das opções: COTAHIST da B3 (fim de dia). Atenção ao que essa fonte NÃO traz:
  - contratos em aberto  -> a liquidez usa nº de negócios, volume financeiro e spread;
  - estilo da série (americana/europeia) -> aparece como "n/d" salvo se você informar em
    config.json  ->  "opcoes": {"estilos": {"PETRK300": "europeia"}} ;
  - eventos (resultado, data-com) -> não verificados.
Preços de compra/venda (spread) existem só para séries negociadas no último pregão.

Saída: data/options.json.  Posições: edite positions.json (veja o exemplo no próprio arquivo).
Este script apoia a decisão; não é recomendação de investimento.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
from collections import Counter, defaultdict

import requests

from build_data import BRT, CACHE_DIR, DATA_DIR, ATIVOS_DIR, ROOT, deep_merge, dump, load_config, log, r2, r4

OPC_DEFAULT = {
    "put": {"dte_min": 15, "dte_max": 50, "prob_max": 0.15, "spread_max_pct": 5.0, "negocios_min_10p": 5,
            "exigir_suporte": False, "nota_suporte_min": 3.0, "top": 40, "sanidade_strike": [0.4, 1.0]},
    "call": {"dte_min": 30, "dte_max": 120, "spread_max_pct": 8.0, "negocios_min_10p": 5,
             "strike_min_x": 0.95, "strike_max_x": 1.10, "top_por_ticker": 3,
             "peso_liquidez": 0.6, "peso_custo_beneficio": 0.4, "ganho_ref_pct": 150, "dias_ate_alvo_max": 20},
    "call_longa": {"dte_min": 250, "spread_max_pct": 15.0, "negocios_min_10p": 1,
                   "strike_min_x": 0.90, "strike_max_x": 1.10, "cenarios_pct": [-20, 0, 20, 50]},
    "liquidez": {"neg_ref": 300, "vol_ref": 1000000, "peso_neg": 0.4, "peso_vol": 0.3, "peso_spread": 0.3},
    "posicoes": {"dias_restantes_alerta": 21},
    "selic_fallback_aa": 15.0,
    "estilos": {},
}


# --------------------------------------------------------------------------- Black-Scholes
def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S, K, T, r, sig, tipo):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if tipo == "C" else max(K - S, 0.0)
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    if tipo == "C":
        return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1)


def bs_greeks(S, K, T, r, sig, tipo):
    """delta, theta por dia (R$), prob. neutra a risco de terminar ITM."""
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    disc = K * math.exp(-r * T)
    if tipo == "C":
        delta = ncdf(d1)
        theta = -(S * npdf(d1) * sig) / (2 * math.sqrt(T)) - r * disc * ncdf(d2)
        itm = ncdf(d2)
    else:
        delta = ncdf(d1) - 1.0
        theta = -(S * npdf(d1) * sig) / (2 * math.sqrt(T)) + r * disc * ncdf(-d2)
        itm = ncdf(-d2)
    return delta, theta / 365.0, itm


def implied_vol(price, S, K, T, r, tipo):
    if price is None or price <= 0 or T <= 0:
        return None
    lo, hi = 0.01, 4.0
    if bs_price(S, K, T, r, lo, tipo) - 1e-9 > price or bs_price(S, K, T, r, hi, tipo) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, tipo) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------- utilidades
def fetch_selic(fallback):
    try:
        resp = requests.get("https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json",
                            timeout=30)
        resp.raise_for_status()
        return float(resp.json()[-1]["valor"]), "Banco Central, SGS 432 (meta Selic)"
    except Exception as exc:  # noqa: BLE001
        log(f"   AVISO: não foi possível ler a Selic ({exc}); usando {fallback}% a.a. como valor padrão")
        return fallback, "valor padrão do config (BCB indisponível)"


def liq_score(neg10, vol10, spread_pct, spread_max, lq):
    a = min(1.0, math.log1p(neg10) / math.log1p(lq["neg_ref"]))
    b = min(1.0, math.log1p(vol10) / math.log1p(lq["vol_ref"]))
    c = 0.0 if spread_pct is None else max(0.0, 1.0 - spread_pct / (2.0 * spread_max))
    return round(100 * (lq["peso_neg"] * a + lq["peso_vol"] * b + lq["peso_spread"] * c))


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def pct(x):
    return f"{x:.1f}".replace(".", ",")


def nota_txt(x):
    return f"{x:.1f}".replace(".", ",")


def brl(x):
    return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------- séries
def build_series(rows, ultimo, cfg_o, ticker_by_prefix, spots):
    """Agrupa as linhas por série e anexa liquidez (10 pregões) e cotação do último pregão."""
    dates = sorted({r["data"] for r in rows})
    win10 = set(dates[-10:])
    by = defaultdict(list)
    for r in rows:
        by[r["cod"]].append(r)
    last_day = dt.date.fromisoformat(ultimo)
    out = {}
    for cod, rs in by.items():
        t = ticker_by_prefix.get(cod[:4])
        if not t or t not in spots:
            continue
        rs.sort(key=lambda x: x["data"])
        r10 = [x for x in rs if x["data"] in win10]
        latest = next((x for x in reversed(rs) if x["data"] == ultimo), None)
        ref = latest or rs[-1]
        venc = dt.date.fromisoformat(ref["ven"])
        out[cod] = {
            "cod": cod, "ticker": t, "tp": ref["tp"], "strike": ref["exe"], "venc": ref["ven"],
            "dte": (venc - last_day).days, "neg10": sum(x["neg"] for x in r10),
            "vol10": sum(x["vol"] for x in r10), "latest": latest,
            "estilo": cfg_o["estilos"].get(cod, "n/d"),
        }
    return out


def enrich(s, spot, hv, rate, lq, spread_max):
    """Preços, spread, IV, gregas. Devolve None se não houver cotação do último pregão."""
    q = s["latest"]
    if not q:
        return None
    bid, ask, last = q["ofc"], q["ofv"], q["ult"]
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else None
    spread = (ask - bid) / mid * 100 if mid else None
    T = max(s["dte"], 0) / 365.0
    ref = mid if mid else (last if last > 0 else None)
    iv = implied_vol(ref, spot, s["strike"], T, rate, s["tp"])
    sig = iv or hv or 0.35
    if T > 0:
        delta, theta, itm = bs_greeks(spot, s["strike"], T, rate, sig, s["tp"])
    else:
        delta = theta = None
        itm = 1.0 if (spot > s["strike"] if s["tp"] == "C" else spot < s["strike"]) else 0.0
    return {"bid": bid, "ask": ask, "last": last, "mid": mid, "spread": spread, "iv": iv, "sig": sig,
            "delta": delta, "theta": theta, "prob_itm": itm, "T": T,
            "liq": liq_score(s["neg10"], s["vol10"], spread, spread_max, lq)}


# --------------------------------------------------------------------------- módulo 2: PUTs
def melhor_zona_suporte(zonas, spot, strike, nota_min):
    cand = [z for z in zonas if z["nota"] >= nota_min and z["hi"] < spot * 0.995 and strike <= z["lo"]]
    return max(cand, key=lambda z: z["nota"]) if cand else None


def put_obs(it, spot):
    fav, atn = [], []
    fav.append(f"strike {pct(it['dist_pct'])}% abaixo do preço ({brl(spot)})")
    fav.append(f"chance de exercício ≈ {pct(it['prob_ex'] * 100)}%")
    if it["fora_2s"]:
        fav.append("strike fora da faixa de 2 desvios até o vencimento")
    elif it["fora_1s"]:
        fav.append("strike fora de 1 desvio (mas dentro de 2)")
    else:
        atn.append("strike dentro da faixa de 1 desvio até o vencimento")
    if it["suporte"]:
        fav.append(f"abaixo de zona de suporte (nota {nota_txt(it['suporte']['nota'])}: {', '.join(it['suporte']['fatores'][:3])})")
    else:
        atn.append("sem zona de suporte forte acima do strike")
    if it["iv"] and it["hv"]:
        if it["iv"] > it["hv"] * 1.1:
            fav.append(f"volatilidade implícita {pct(it['iv'] * 100)}% acima da histórica {pct(it['hv'] * 100)}% (prêmio mais caro, favorece quem vende)")
        elif it["iv"] < it["hv"] * 0.9:
            atn.append(f"volatilidade implícita {pct(it['iv'] * 100)}% abaixo da histórica {pct(it['hv'] * 100)}% (prêmio com pouca folga)")
    if it["spread"] is not None and it["spread"] > 3:
        atn.append(f"spread de {pct(it['spread'])}%")
    if it["estilo"] == "n/d":
        atn.append("estilo da série não informado: se for americana, o exercício antecipado é possível")
    atn.append("resultados e data-com não verificados")
    comp = f"Se exercido, você compra a {brl(it['strike'])} (equilíbrio {brl(it['equilibrio'])})"
    return f"Favorece: {'; '.join(fav)}. Atenção: {'; '.join(atn)}. {comp}."


def rank_puts(series, spots, cfg_o, rate):
    po, lq = cfg_o["put"], cfg_o["liquidez"]
    excl = Counter()
    itens = []
    for s in series.values():
        if s["tp"] != "P":
            continue
        a = spots[s["ticker"]]
        spot = a["ultimo"]["close"]
        hv = a["vol"]["hist60"] or a["vol"]["hist20"]
        if not (po["dte_min"] <= s["dte"] <= po["dte_max"]):
            excl["prazo fora de 15–50 dias"] += 1
            continue
        if not s["latest"]:
            excl["sem negócio no último pregão (sem cotação atual)"] += 1
            continue
        if not (po["sanidade_strike"][0] * spot <= s["strike"] <= po["sanidade_strike"][1] * spot):
            excl["strike muito distante do preço"] += 1
            continue
        e = enrich(s, spot, hv, rate, lq, po["spread_max_pct"])
        if not e or e["bid"] <= 0 or e["ask"] <= 0:
            excl["sem oferta de compra ou de venda"] += 1
            continue
        if e["spread"] > po["spread_max_pct"]:
            excl["spread acima do limite"] += 1
            continue
        if s["neg10"] < po["negocios_min_10p"]:
            excl["poucos negócios (10 pregões)"] += 1
            continue
        prob = e["prob_itm"]
        if prob > po["prob_max"]:
            excl["chance de exercício acima do limite"] += 1
            continue
        sup = melhor_zona_suporte(a["zonas"], spot, s["strike"], po["nota_suporte_min"])
        if po["exigir_suporte"] and not sup:
            excl["sem zona de suporte acima do strike"] += 1
            continue
        premio = e["bid"]  # venda a mercado pela melhor oferta de compra
        ret = premio / s["strike"]
        sd = (hv or e["sig"]) * math.sqrt(s["dte"] / 365.0)
        it = {
            "ticker": s["ticker"], "serie": s["cod"], "strike": r2(s["strike"]), "venc": s["venc"], "dte": s["dte"],
            "spot": r2(spot), "premio": r2(premio), "bid": r2(e["bid"]), "ask": r2(e["ask"]),
            "ret_pct": r2(ret * 100), "ret_anual_pct": r2(((1 + ret) ** (365.0 / s["dte"]) - 1) * 100),
            "equilibrio": r2(s["strike"] - premio), "dist_pct": r2((spot - s["strike"]) / spot * 100),
            "prob_ex": r4(prob), "delta": r4(e["delta"]), "iv": r4(e["iv"]), "hv": r4(hv),
            "spread": r2(e["spread"]), "neg10": s["neg10"], "vol10": round(s["vol10"]), "liq": e["liq"],
            "faixa_1s": [r2(spot * (1 - sd)), r2(spot * (1 + sd))],
            "faixa_2s": [r2(spot * (1 - 2 * sd)), r2(spot * (1 + 2 * sd))],
            "fora_1s": s["strike"] < spot * (1 - sd), "fora_2s": s["strike"] < spot * (1 - 2 * sd),
            "suporte": None if not sup else {"nota": sup["nota"], "zona": [sup["lo"], sup["hi"]], "fatores": sup["fatores"]},
            "estilo": s["estilo"],
        }
        it["obs"] = put_obs(it, spot)
        itens.append(it)
    itens.sort(key=lambda x: -x["ret_anual_pct"])
    for i, it in enumerate(itens, 1):
        it["rank"] = i
    return itens[: po["top"]], dict(excl), len([1 for s in series.values() if s["tp"] == "P"])


# --------------------------------------------------------------------------- módulo 1: CALLs de entrada
def call_candidates(ticker, a, alvos, series, cfg_o, rate):
    co, lq = cfg_o["call"], cfg_o["liquidez"]
    spot = a["ultimo"]["close"]
    hv = a["vol"]["hist60"] or a["vol"]["hist20"]
    excl = Counter()
    res = []
    for s in series.values():
        if s["ticker"] != ticker or s["tp"] != "C":
            continue
        if not (co["dte_min"] <= s["dte"] <= co["dte_max"]):
            excl["prazo fora do intervalo"] += 1
            continue
        if not (co["strike_min_x"] * spot <= s["strike"] <= co["strike_max_x"] * spot):
            excl["strike longe do dinheiro"] += 1
            continue
        if not s["latest"]:
            excl["sem negócio no último pregão"] += 1
            continue
        e = enrich(s, spot, hv, rate, lq, co["spread_max_pct"])
        if not e or e["bid"] <= 0 or e["ask"] <= 0:
            excl["sem oferta de compra ou de venda"] += 1
            continue
        if e["spread"] > co["spread_max_pct"]:
            excl["spread acima do limite"] += 1
            continue
        if s["neg10"] < co["negocios_min_10p"]:
            excl["poucos negócios (10 pregões)"] += 1
            continue
        custo = e["ask"]
        be = s["strike"] + custo
        dias_alvo = min(max(s["dte"] // 3, 1), co["dias_ate_alvo_max"])
        T_rem = max(s["dte"] - dias_alvo, 0) / 365.0
        alvo_vals = []
        for al in alvos:
            val = bs_price(al["p"], s["strike"], T_rem, rate, e["sig"], "C")
            alvo_vals.append({"alvo": al["p"], "rot": al["rot"], "valor_est": r2(val),
                              "ganho_pct": r2((val / custo - 1) * 100), "dias_considerados": dias_alvo})
        g1 = alvo_vals[0]["ganho_pct"] if alvo_vals else 0.0
        cb = min(1.0, max(0.0, g1 / co["ganho_ref_pct"]))
        score = co["peso_liquidez"] * e["liq"] / 100 + co["peso_custo_beneficio"] * cb
        res.append({
            "serie": s["cod"], "strike": r2(s["strike"]), "venc": s["venc"], "dte": s["dte"],
            "ask": r2(custo), "bid": r2(e["bid"]), "perda_max": r2(custo), "perda_max_lote100": r2(custo * 100),
            "equilibrio": r2(be), "mov_ate_equilibrio_pct": r2((be / spot - 1) * 100),
            "delta": r4(e["delta"]), "theta_dia": r2(e["theta"]), "theta_pct_dia": r2((e["theta"] or 0) / custo * 100),
            "iv": r4(e["iv"]), "hv": r4(hv), "spread": r2(e["spread"]), "custo_ida_volta_pct": r2(e["spread"]),
            "neg10": s["neg10"], "vol10": round(s["vol10"]), "liq": e["liq"], "estilo": s["estilo"],
            "alvos": alvo_vals, "score": r4(score),
        })
    res.sort(key=lambda x: -x["score"])
    return res[: co["top_por_ticker"]], dict(excl)


def call_obs(al, c):
    fav = [f"zona nota {nota_txt(al['nota'])} ({', '.join(al['fatores'][:3])})"]
    if al["gatilhos"]:
        fav.append("gatilhos: " + ", ".join(al["gatilhos"]))
    fav.append(f"liquidez {c['liq']}/100, spread {pct(c['spread'])}%")
    atn = [f"precisa subir {pct(c['mov_ate_equilibrio_pct'])}% até o equilíbrio", f"perde cerca de {brl(abs(c['theta_dia']))} por dia de tempo"]
    if c["iv"] and c["hv"] and c["iv"] > c["hv"] * 1.15:
        atn.append(f"volatilidade implícita {pct(c['iv'] * 100)}% acima da histórica {pct(c['hv'] * 100)}%: opção cara")
    if c["estilo"] == "n/d":
        atn.append("estilo da série não informado")
    if not c["alvos"]:
        atn.append("sem alvos acima do preço calculados")
    return f"Favorece: {'; '.join(fav)}. Atenção: {'; '.join(atn)}."


# --------------------------------------------------------------------------- módulo 3: CALLs longas
def calls_longas(quedas, spots, series, cfg_o, rate):
    cl, lq = cfg_o["call_longa"], cfg_o["liquidez"]
    out = []
    for q in quedas:
        t = q["ticker"]
        a = spots.get(t)
        if not a:
            continue
        spot = a["ultimo"]["close"]
        hv = a["vol"]["hist60"] or a["vol"]["hist20"]
        longas = [s for s in series.values() if s["ticker"] == t and s["tp"] == "C" and s["dte"] >= cl["dte_min"]]
        melhor, motivos = None, Counter()
        for s in sorted(longas, key=lambda x: abs(x["strike"] - spot)):
            if not (cl["strike_min_x"] * spot <= s["strike"] <= cl["strike_max_x"] * spot):
                motivos["strike longe do dinheiro"] += 1
                continue
            if not s["latest"]:
                motivos["sem negócio no último pregão"] += 1
                continue
            e = enrich(s, spot, hv, rate, lq, cl["spread_max_pct"])
            if not e or e["ask"] <= 0:
                motivos["sem oferta de venda"] += 1
                continue
            if e["spread"] is not None and e["spread"] > cl["spread_max_pct"]:
                motivos["spread acima do limite"] += 1
                continue
            if s["neg10"] < cl["negocios_min_10p"]:
                motivos["poucos negócios"] += 1
                continue
            custo = e["ask"]
            cen = []
            for x in cl["cenarios_pct"]:
                px = spot * (1 + x / 100)
                pay = max(px - s["strike"], 0) - custo
                cen.append({"mov_pct": x, "preco": r2(px), "resultado_pct": r2(pay / custo * 100), "acao_pct": x})
            melhor = {
                "serie": s["cod"], "strike": r2(s["strike"]), "venc": s["venc"], "dte": s["dte"], "ask": r2(custo),
                "bid": r2(e["bid"]), "equilibrio": r2(s["strike"] + custo),
                "mov_ate_equilibrio_pct": r2(((s["strike"] + custo) / spot - 1) * 100),
                "delta": r4(e["delta"]), "iv": r4(e["iv"]), "hv": r4(hv), "spread": r2(e["spread"]),
                "neg10": s["neg10"], "liq": e["liq"], "estilo": s["estilo"], "cenarios": cen,
                "perda_max": r2(custo),
            }
            break
        obs = ""
        if melhor:
            obs = (f"Queda de {pct(abs(q['da_max_52s_pct']))}% desde a máxima de 52 semanas. "
                   f"Estabilização: {', '.join(q['estabilizacao']) or 'nenhum sinal ainda'}. "
                   f"Atenção: juros altos encarecem a CALL longa; fundamentos não verificados.")
        else:
            obs = (f"Sem CALL longa (≥ {cl['dte_min']} dias) líquida perto do dinheiro nos dados. "
                   f"Séries longas encontradas: {len(longas)}.")
        out.append({"ticker": t, "preco": r2(spot), "da_max_52s_pct": q["da_max_52s_pct"], "max_52s": q["max_52s"],
                    "estabilizacao": q["estabilizacao"], "estrutura": q["estrutura"], "semanal": q["semanal"],
                    "serie_longa": melhor, "series_longas_total": len(longas), "motivos": dict(motivos), "obs": obs})
    return out


# --------------------------------------------------------------------------- módulo 4: posições
def posicoes(cfg_o, series, spots, rate):
    arq = load_json(os.path.join(ROOT, "positions.json"), {"posicoes": []})
    lista = arq.get("posicoes", []) if isinstance(arq, dict) else arq
    out = []
    for p in lista:
        t = str(p.get("ticker", "")).upper()
        serie = str(p.get("serie", "")).upper()
        s = series.get(serie)
        a = spots.get(t)
        item = {"ticker": t, "serie": serie, "lado": p.get("lado"), "qtd": p.get("qtd"),
                "preco_entrada": p.get("preco_entrada"), "data_entrada": p.get("data_entrada"), "alertas": []}
        if not a or not s or not s["latest"]:
            item["alertas"].append("série ou ativo sem cotação nos dados do último pregão")
            out.append(item)
            continue
        spot = a["ultimo"]["close"]
        hv = a["vol"]["hist60"] or a["vol"]["hist20"]
        e = enrich(s, spot, hv, rate, cfg_o["liquidez"], cfg_o["call"]["spread_max_pct"])
        comprada = str(p.get("lado", "")).lower().startswith("compr")
        atual = (e["bid"] if comprada else e["ask"]) or e["last"]
        ent = float(p.get("preco_entrada") or 0)
        qtd = float(p.get("qtd") or 0)
        pl = ((atual - ent) if comprada else (ent - atual)) * qtd
        item.update({"tp": "CALL" if s["tp"] == "C" else "PUT", "strike": r2(s["strike"]), "venc": s["venc"],
                     "dte": s["dte"], "spot": r2(spot), "preco_atual": r2(atual),
                     "resultado_rs": r2(pl), "resultado_pct": r2(pl / (ent * qtd) * 100) if ent * qtd else None,
                     "spread": r2(e["spread"]), "liq": e["liq"], "estilo": s["estilo"], "iv": r4(e["iv"])})
        if s["tp"] == "C" and comprada:
            alv = [x["p"] for x in a["alvos"]]
            a1 = p.get("alvo1") or (alv[0] if alv else None)
            a2 = p.get("alvo2") or (alv[1] if len(alv) > 1 else None)
            item["alvos"] = {"alvo1": a1, "alvo2": a2}
            item["equilibrio"] = r2(s["strike"] + ent)
            if a2 and spot >= a2:
                item["alertas"].append(f"Alvo 2 atingido ({brl(a2)}): realizar o restante ou subir a saída")
            elif a1 and spot >= a1:
                item["alertas"].append(f"Alvo 1 atingido ({brl(a1)}): considerar realizar parte (sugestão inicial: 50%)")
            if any(g == "CHoCH de baixa recente" for g in a["gatilhos"]):
                item["alertas"].append("Estrutura contra a posição (CHoCH de baixa no diário): reavaliar")
            if s["dte"] <= cfg_o["posicoes"]["dias_restantes_alerta"]:
                item["alertas"].append(f"Pouco prazo ({s['dte']} dias): o valor extrínseco cai rápido")
            if e["spread"] and e["spread"] > cfg_o["call"]["spread_max_pct"]:
                item["alertas"].append(f"Spread alto ({pct(e['spread'])}%): sair está mais caro")
        elif s["tp"] == "P" and not comprada:
            item["equilibrio"] = r2(s["strike"] - ent)
            item["dist_strike_pct"] = r2((spot - s["strike"]) / spot * 100)
            item["prob_ex"] = r4(e["prob_itm"])
            item["alertas"].append("Sem regra de saída (você entra sabendo onde aceita comprar a ação)")
            if s["estilo"] == "n/d":
                item["alertas"].append("Estilo da série não informado: exercício antecipado possível se for americana")
            elif s["estilo"] == "americana":
                item["alertas"].append("Série americana: exercício antecipado possível")
        out.append(item)
    return out


# --------------------------------------------------------------------------- principal
def main():
    cfg = load_config()
    cfg_o = deep_merge(json.loads(json.dumps(OPC_DEFAULT)), cfg.get("opcoes", {}))
    rows = load_json(os.path.join(CACHE_DIR, "options_rows.json"))
    uni = load_json(os.path.join(CACHE_DIR, "universo.json"))
    if not rows or not uni:
        log("ERRO: rode build_data.py antes (faltam .cache/options_rows.json e .cache/universo.json).")
        sys.exit(2)
    ultimo = uni["ultimo_pregao"]
    ticker_by_prefix = uni["prefixos"]

    spots = {}
    for t in ticker_by_prefix.values():
        a = load_json(os.path.join(ATIVOS_DIR, f"{t}.json"))
        if a:
            spots[t] = a
    selic, selic_fonte = fetch_selic(cfg_o["selic_fallback_aa"])
    rate = math.log(1 + selic / 100.0)
    log(f"Selic {selic}% a.a. ({selic_fonte}); pregão {ultimo}; {len(spots)} ativos")

    series = build_series(rows, ultimo, cfg_o, ticker_by_prefix, spots)
    cotadas = sum(1 for s in series.values() if s["latest"])
    log(f"{len(series)} séries, {cotadas} com cotação no último pregão")

    puts, excl, total_puts = rank_puts(series, spots, cfg_o, rate)
    log(f"PUTs no ranking: {len(puts)} (de {total_puts} séries de PUT)")

    alertas = (load_json(os.path.join(DATA_DIR, "alertas.json"), {}) or {}).get("itens", [])
    entradas = []
    for al in alertas:
        a = spots.get(al["ticker"])
        if not a:
            continue
        cands, ex = call_candidates(al["ticker"], a, al.get("alvos", []), series, cfg_o, rate)
        for c in cands:
            c["obs"] = call_obs(al, c)
        entradas.append({"ticker": al["ticker"], "preco": al["preco"], "zona": al["zona"], "nota": al["nota"],
                         "fatores": al["fatores"], "gatilhos": al["gatilhos"], "alvos": al.get("alvos", []),
                         "series": cands, "excluidas": ex})

    quedas = (load_json(os.path.join(DATA_DIR, "quedas.json"), {}) or {}).get("itens", [])
    longas = calls_longas(quedas, spots, series, cfg_o, rate)
    pos = posicoes(cfg_o, series, spots, rate)

    now = dt.datetime.now(BRT)
    dump(os.path.join(DATA_DIR, "options.json"), {
        "ultimo_pregao": ultimo, "gerado_em": now.isoformat(timespec="minutes"),
        "selic": {"aa": selic, "fonte": selic_fonte},
        "premissas": [
            "Preços de compra e venda são os do fim do último pregão; séries sem negócio no dia ficam sem cotação.",
            "Volatilidade implícita e gregas calculadas por Black-Scholes (estilo europeu, sem dividendos).",
            "Contratos em aberto não existem nesta fonte: a liquidez usa negócios, volume e spread.",
            "Estilo da série (americana/europeia) não vem da fonte: aparece como n/d até você informar no config.json.",
            "Retorno da PUT = prêmio (melhor oferta de compra) sobre o strike; a garantia real exigida pela B3 pode diferir.",
        ],
        "puts": {"itens": puts, "excluidos": excl, "total_series_put": total_puts,
                 "filtros": {k: cfg_o["put"][k] for k in ("dte_min", "dte_max", "prob_max", "spread_max_pct", "negocios_min_10p")}},
        "entradas_call": entradas, "calls_longas": longas, "posicoes": pos,
    })
    log("options.json gravado.")


if __name__ == "__main__":
    main()
