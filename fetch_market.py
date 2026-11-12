#!/usr/bin/env python3
"""
Stáhne denní ceny akcií, splity a kurzy (ECB + ČNB) pro všechny tickery z portfolia
a uloží je do Gistu jako portfolio_market.json. Spouští GitHub Action (daily.yml).

Potřebné proměnné prostředí (Settings → Secrets and variables → Actions):
  GIST_TOKEN  – GitHub token s právem zapisovat Gisty
  GIST_ID     – ID Gistu s daty portfolia
"""
import datetime as dt
import json
import os
import sys

import requests
import yfinance as yf

TOKEN = os.environ["GIST_TOKEN"]
GIST_ID = os.environ["GIST_ID"]
PORTFOLIO_FILE = "portfolio_tracker_data.json"
MARKET_FILE = "portfolio_market.json"
API = f"https://api.github.com/gists/{GIST_ID}"
HDR = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
FX_CURS = ["CZK", "EUR", "GBP", "CHF", "PLN"]
CNB_CURS = ["USD", "EUR", "GBP", "CHF", "PLN"]
# XTB suffix -> Yahoo suffix (fallback when the app didn't store a Yahoo symbol)
XTB_TO_YAHOO = {"NL": "AS", "DE": "DE", "UK": "L", "FR": "PA", "IT": "MI", "ES": "MC", "BE": "BR",
                "PT": "LS", "FI": "HE", "CH": "SW", "PL": "WA", "CZ": "PR", "DK": "CO", "SE": "ST",
                "NO": "OL", "AT": "VI"}
TODAY = dt.date.today()
# Benchmarks (ETFs tracking the index, price only – same basis as the portfolio returns)
BENCHMARKS = {"SP500": "SPY", "MSCIWORLD": "URTH"}


def log(*a):
    print(*a, flush=True)


def gist_file(gist, name):
    f = gist.get("files", {}).get(name)
    if not f:
        return None
    text = f.get("content") or ""
    if f.get("truncated"):  # GitHub API cuts files over ~1 MB, the raw URL has the full content
        text = requests.get(f["raw_url"], headers=HDR, timeout=60).text
    return json.loads(text) if text.strip() else None


def default_yahoo(ticker):
    if "." not in ticker:
        return ticker
    base, suf = ticker.rsplit(".", 1)
    return f"{base}.{XTB_TO_YAHOO[suf]}" if suf in XTB_TO_YAHOO else ticker


def collect_tickers(port):
    """ticker -> (yahoo symbol, earliest date needed). Includes sold positions for history."""
    ymap = port.get("yahooMap") or {}
    out = {}

    def add(t, date, yahoo):
        if not t or not date:
            return
        sym = ymap.get(t) or yahoo or default_yahoo(t)
        if t not in out:
            out[t] = [sym, date]
        else:
            if date < out[t][1]:
                out[t][1] = date
            if ymap.get(t):
                out[t][0] = ymap[t]

    for b in port.get("buys", []):
        add(b.get("ticker"), b.get("buyDate"), b.get("yahoo"))
    for s in port.get("sells", []):
        lots = s.get("lots") or []
        for l in lots:
            add(s.get("ticker"), l.get("buyDate") or s.get("date"), s.get("yahoo"))
        if not lots:
            add(s.get("ticker"), s.get("date"), s.get("yahoo"))
    return out


def fetch_prices(tickers, total_return=False):
    prices, splits, missing = {}, {}, {}
    for t, (sym, start) in sorted(tickers.items()):
        try:
            tk = yf.Ticker(sym)
            start_d = dt.date.fromisoformat(start) - dt.timedelta(days=10)
            hist = tk.history(start=start_d.isoformat(), auto_adjust=False, actions=True)
            if hist is None or hist.empty:
                raise ValueError("žádná data")
            cur = (tk.history_metadata or {}).get("currency") or "USD"
            div = 1.0
            if cur in ("GBp", "GBX"):
                cur, div = "GBP", 100.0
            elif cur == "ZAc":
                cur, div = "ZAR", 100.0
            elif cur == "ILA":
                cur, div = "ILS", 100.0
            px = [[idx.strftime("%Y-%m-%d"), round(float(c) / div, 4)]
                  for idx, c in hist["Close"].dropna().items()]
            prices[t] = {"yahoo": sym, "cur": cur, "px": px}
            if total_return and "Adj Close" in hist:   # dividends reinvested – for a fair comparison incl. dividends
                prices[t]["tr"] = [[idx.strftime("%Y-%m-%d"), round(float(c) / div, 4)] for idx, c in hist["Adj Close"].dropna().items()]
            if "Stock Splits" in hist:
                sp = [[idx.strftime("%Y-%m-%d"), float(r)] for idx, r in hist["Stock Splits"].items() if r and r > 0]
                if sp:
                    splits[t] = sp
            log(f"✓ {t:12} {sym:12} {cur}  {len(px)} dní")
        except Exception as e:  # noqa: BLE001
            missing[t] = sym
            log(f"✗ {t:12} {sym:12} {e}")
    return prices, splits, missing


def fetch_ecb(start, old):
    """Frankfurter (ECB) in 90-day chunks so every business day is returned. USD base."""
    out = dict(old or {})
    d = start
    while d <= TODAY:
        e = min(d + dt.timedelta(days=89), TODAY)
        try:
            r = requests.get(f"https://api.frankfurter.app/{d}..{e}",
                             params={"from": "USD", "to": ",".join(FX_CURS)}, timeout=30)
            r.raise_for_status()
            out.update(r.json().get("rates", {}))
        except Exception as ex:  # noqa: BLE001
            log(f"ECB {d}..{e} selhalo: {ex}")
        d = e + dt.timedelta(days=1)
    return out


def fetch_cnb_year(year):
    url = ("https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/"
           f"kurzy-devizoveho-trhu/rok.txt?rok={year}")
    txt = requests.get(url, timeout=30).text
    out, header = {}, None
    for line in txt.splitlines():
        parts = line.strip().split("|")
        if not parts or not parts[0]:
            continue
        if parts[0].lower().startswith("datum"):
            header = parts
            continue
        if not header:
            continue
        try:
            day = dt.datetime.strptime(parts[0], "%d.%m.%Y").date().isoformat()
        except ValueError:
            continue
        rates = {}
        for h, v in zip(header[1:], parts[1:]):
            try:
                amount, code = h.strip().split(" ")
                rates[code] = float(v.replace(",", ".")) / float(amount)
            except ValueError:
                pass
        out[day] = {k: rates[k] for k in CNB_CURS if k in rates}
    return out


def fetch_cnb(start, old):
    out = dict(old or {})
    for year in range(start.year, TODAY.year + 1):
        # past years are final – download only if we don't have them yet
        have = any(k.startswith(f"{year}-12") for k in out)
        if year < TODAY.year and have:
            continue
        try:
            out.update(fetch_cnb_year(year))
            log(f"ČNB {year} ✓")
        except Exception as ex:  # noqa: BLE001
            log(f"ČNB {year} selhalo: {ex}")
    return out


def rate_on(series, day, cur):
    keys = sorted(k for k in series if k <= day)
    return series[keys[-1]].get(cur) if keys else None


def snapshot(port, prices, ecb):
    """Today's value of open positions – stored as a daily record."""
    val_usd, cost_czk = 0.0, 0.0
    day = TODAY.isoformat()
    for b in port.get("buys", []):
        p = prices.get(b.get("ticker"))
        if p and p["px"]:
            close, cur = p["px"][-1][1], p["cur"]
            px_usd = close if cur == "USD" else close / (rate_on(ecb, day, cur) or 1)
        else:
            px_usd = b.get("buyPrice") or 0
        val_usd += (b.get("shares") or 0) * px_usd
        cost_czk += (b.get("shares") or 0) * (b.get("buyPrice") or 0) * (b.get("buyForex") or 0)
    czk = rate_on(ecb, day, "CZK") or 0
    return {"date": day, "valueUSD": round(val_usd, 2), "valueCZK": round(val_usd * czk, 2),
            "costCZK": round(cost_czk, 2)}


def main():
    r = requests.get(API, headers=HDR, timeout=30)
    if r.status_code == 404:
        sys.exit("Gist nenalezen – zkontroluj secret GIST_ID a oprávnění tokenu.")
    r.raise_for_status()
    gist = r.json()
    port = gist_file(gist, PORTFOLIO_FILE)
    if not port:
        sys.exit(f"V Gistu chybí {PORTFOLIO_FILE} – nejdřív ulož portfolio z aplikace.")
    market = gist_file(gist, MARKET_FILE) or {}

    tickers = collect_tickers(port)
    dates = [v[1] for v in tickers.values()] + [d.get("date") for d in port.get("dividends", []) if d.get("date")]
    start = dt.date.fromisoformat(min(dates)) if dates else TODAY - dt.timedelta(days=365)
    log(f"{len(tickers)} tickerů, historie od {start}")

    prices, splits, missing = fetch_prices(tickers)
    log("Benchmarky:")
    bench, _, _ = fetch_prices({k: (sym, start.isoformat()) for k, sym in BENCHMARKS.items()}, total_return=True)
    ecb = fetch_ecb(start - dt.timedelta(days=10), market.get("ecb"))
    cnb = fetch_cnb(start - dt.timedelta(days=10), market.get("cnb"))

    snaps = {s["date"]: s for s in market.get("snapshots", [])}
    snap = snapshot(port, prices, ecb)
    snaps[snap["date"]] = snap

    market = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "prices": prices,
        "splits": splits,
        "missing": missing,
        "benchmarks": bench,
        "ecb": ecb,
        "cnb": cnb,
        "snapshots": [snaps[k] for k in sorted(snaps)],
    }
    body = {"files": {MARKET_FILE: {"content": json.dumps(market, separators=(",", ":"))}}}
    requests.patch(API, headers=HDR, json=body, timeout=120).raise_for_status()
    log(f"Uloženo: {len(prices)} tickerů, {len(missing)} bez dat, snímek {snap}")


if __name__ == "__main__":
    main()
