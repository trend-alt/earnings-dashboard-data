#!/usr/bin/env python3
"""
Earnings Dashboard — Nifty 100 + S&P 500
Pulls expected result dates and latest quarterly results (YoY revenue and
gross-profit growth) via yfinance, then writes a self-contained dashboard.html.

Run:  python3 earnings_dash.py            (normal daily run)
      python3 earnings_dash.py --full     (ignore cache, refetch everything)
      python3 earnings_dash.py --limit 20 (quick test on 20 stocks per market)
"""
import argparse, io, json, sys, time, warnings, datetime as dt
warnings.filterwarnings("ignore")
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache.json"
UNIVERSE = HERE / "universe.json"
OUT = HERE / "dashboard.html"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36"}
TODAY = (dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)).date()  # IST

# ---------------------------------------------------------------- universe
APP = False   # --app: Nifty 500 only, writes app-data.json (see app_data.py)
NIFTY_URLS = ("https://niftyindices.com/IndexConstituent/ind_nifty100list.csv",
              "https://archives.nseindia.com/content/indices/ind_nifty100list.csv")

def load_universe(refresh_days=7):
    if UNIVERSE.exists():
        u = json.loads(UNIVERSE.read_text())
        age = (TODAY - dt.date.fromisoformat(u["asof"])).days
        if age < refresh_days:
            return u
    u = {"asof": TODAY.isoformat(), "IN": [], "US": []}
    # Nifty 100
    for url in NIFTY_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=20); r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            inds = df["Industry"] if "Industry" in df else [None] * len(df)
            u["IN"] = [{"sym": s.strip() + ".NS", "name": n.strip(), "ind": i.strip() if isinstance(i, str) else None}
                       for s, n, i in zip(df["Symbol"], df["Company Name"], inds)]
            break
        except Exception as e:
            print(f"  Nifty list from {url} failed: {e}")
    # S&P 500
    try:
        if APP: raise RuntimeError("skipped in --app mode")
        r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                         headers=UA, timeout=20); r.raise_for_status()
        df = pd.read_html(io.StringIO(r.text))[0]
        u["US"] = [{"sym": s.replace(".", "-"), "name": n}
                   for s, n in zip(df["Symbol"], df["Security"])]
    except Exception as e:
        if not APP: print(f"  S&P 500 list failed: {e}")
    if UNIVERSE.exists():                       # keep old list for any side that failed
        old = json.loads(UNIVERSE.read_text())
        for k in ("IN", "US"):
            if not u[k]:
                u[k] = old.get(k, [])
    if not u["IN"] and not u["US"]:
        sys.exit("Could not load either stock list. Check internet and retry.")
    UNIVERSE.write_text(json.dumps(u, indent=1))
    return u

# ---------------------------------------------------------------- per-stock fetch
import calendar, re, threading, urllib.parse
SCREENER_LOCK = threading.Semaphore(2)

def _num(x):
    try:
        f = float(x)
        return None if pd.isna(f) else f
    except Exception:
        return None

def _pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100, 1)

def _month_end(y, m):
    return dt.date(y, m, calendar.monthrange(y, m)[1])

def _add_quarter(d):
    m = d.month + 3; y = d.year + (m - 1) // 12; m = (m - 1) % 12 + 1
    return _month_end(y, m)

def dates_for(t):
    """Return (next_date, last_date) as ISO strings or None."""
    nxt = last = None
    try:
        ed = t.get_earnings_dates(limit=8)
        if ed is not None and len(ed):
            ds = sorted({i.date() for i in ed.index})
            fut = [d for d in ds if d >= TODAY]
            past = [d for d in ds if d < TODAY]
            nxt = fut[0] if fut else None
            last = past[-1] if past else None
    except Exception:
        pass
    if nxt is None:
        try:
            cal = t.calendar or {}
            eds = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if eds:
                fut = [d for d in eds if d >= TODAY]
                nxt = fut[0] if fut else max(eds)
        except Exception:
            pass
    if nxt is not None and nxt < TODAY:          # date already passed -> it was reported
        last = max(last, nxt) if last else nxt
        nxt = None
    iso = lambda d: d.isoformat() if d else None
    return iso(nxt), iso(last)

STMT_ROWS = {"rev": ["Total Revenue", "Operating Revenue"], "gp": ["Gross Profit"],
             "ebitda": ["EBITDA"], "op": ["Operating Income"],
             "net": ["Net Income Common Stockholders", "Net Income"]}

def yahoo_hist(t):
    """{quarter_end: {rev, gp, ebitda, op, net}} from statements + fast earnings summary."""
    hist = {}
    q = t.quarterly_income_stmt
    if q is not None and not q.empty:
        for c in q.columns:
            rec = {}
            for k, rows in STMT_ROWS.items():
                for r in rows:
                    if r in q.index and _num(q.at[r, c]) is not None:
                        rec[k] = _num(q.at[r, c]); break
            if rec.get("rev") is not None:
                hist[c.date()] = rec
    # fast summary: aligns by matching revenue with a statement quarter
    try:
        from yfinance.data import YfData
        d = YfData().get_raw_json("https://query2.finance.yahoo.com/v10/finance/quoteSummary/" + t.ticker,
                                  params={"modules": "earnings"})
        ch = d["quoteSummary"]["result"][0]["earnings"]["financialsChart"]["quarterly"]
        ch = [(x["revenue"].get("raw"), x["earnings"].get("raw")) for x in ch]
        anchor = None
        for i, (rv, _) in enumerate(ch):
            for qd, rec in hist.items():
                if rv and abs(rec["rev"] - rv) <= abs(rv) * 0.001:
                    anchor = (i, qd)
        if anchor:
            i, qd = anchor
            for rv, net in ch[i + 1:]:
                qd = _add_quarter(qd)
                if qd not in hist and rv:
                    hist[qd] = {"rev": float(rv), "net": float(net) if net is not None else None}
    except Exception:
        pass
    return hist

def screener_hist(code):
    """India: {quarter_end: {rev, op, net}} in rupees, from Screener.in quarterly table."""
    MON = {m: i for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
    for path in ("consolidated/", ""):
        with SCREENER_LOCK:
            h = requests.get("https://www.screener.in/company/%s/%s" % (urllib.parse.quote(code, safe=""), path),
                             headers=UA, timeout=25).text
            time.sleep(0.6)
        if 'id="quarters"' not in h:
            continue
        sec = h[h.index('id="quarters"'):]; sec = sec[:sec.index("</table>")]
        hdr = re.findall(r"<th[^>]*>\s*([A-Z][a-z]{2}) (\d{4})\s*</th>", sec)
        qs = [_month_end(int(y), MON[m]) for m, y in hdr]
        rows = {}
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", sec, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 2: continue
            label = re.sub(r"<[^>]+>|&nbsp;|\+", " ", tds[0]).strip()
            vals = [re.sub(r"<[^>]+>|,|%", "", v).strip() for v in tds[1:]]
            rows[label] = vals[-len(qs):] if qs else vals
        def get(*labels):
            for l in labels:
                if l in rows: return rows[l]
            return None
        rev, op, net = get("Sales", "Revenue"), get("Operating Profit"), get("Net Profit")
        if not qs or not rev: continue
        bank = "Financing Profit" in rows
        hist = {}
        for i, qd in enumerate(qs):
            f = lambda arr: (_num(arr[i]) * 1e7 if arr and i < len(arr) and arr[i] not in ("", None) and _num(arr[i]) is not None else None)
            hist[qd] = {"rev": f(rev), ("net" if bank else "op"): f(net if bank else op), "net": f(net)}
        return hist, bank
    return {}, False

PROFIT_ORDER = [("gp", "Gross profit"), ("ebitda", "EBITDA"), ("op", "Operating profit"), ("net", "Net profit")]

def growth(hist, order=PROFIT_ORDER):
    if not hist: return {}
    qs = sorted(q for q in hist if hist[q].get("rev") is not None)
    if not qs: return {}
    latest = qs[-1]
    yago = next((q for q in reversed(qs[:-1]) if 340 <= (latest - q).days <= 395), None)
    L, Y = hist[latest], hist.get(yago, {})
    out = {"q_end": latest.isoformat(), "rev": L["rev"], "rev_prev": Y.get("rev"),
           "prof": None, "prof_prev": None, "prof_label": None}
    pk = None
    for k, label in order:                          # same metric in both quarters
        if L.get(k) is not None and Y.get(k) is not None:
            out.update(prof=L[k], prof_prev=Y[k], prof_label=label); pk = k; break
    else:
        for k, label in order:
            if L.get(k) is not None:
                out.update(prof=L[k], prof_label=label); pk = k; break
    out["rev_yoy"] = _pct(out["rev"], out["rev_prev"])
    out["prof_yoy"] = _pct(out["prof"], out["prof_prev"])
    # QoQ: vs the immediately preceding quarter
    pq = next((q for q in reversed(qs[:-1]) if 75 <= (latest - q).days <= 100), None)
    P = hist.get(pq, {})
    out["rev_qoq"] = _pct(L.get("rev"), P.get("rev"))
    out["prof_qoq"] = _pct(L.get(pk), P.get(pk)) if pk else None
    out["q"] = [[q.isoformat(), hist[q].get("rev"), hist[q].get(pk) if pk else None] for q in qs[-8:]]
    return out

def results_for(t, mkt):
    if mkt == "IN":
        try:
            hist, bank = screener_hist(t.ticker.replace(".NS", ""))
            if hist:
                order = [("net", "Net profit")] if bank else [("op", "Operating profit"), ("net", "Net profit")]
                g = growth(hist, order)
                if g: g["src"] = "Screener"; return g
        except Exception:
            pass
    g = growth(yahoo_hist(t))
    if g: g["src"] = "Yahoo"
    return g

def needs_refresh(rec, full):
    if full or "q_end" not in rec or "src" not in rec or "rev_qoq" not in rec or "q" not in rec:
        return True
    last = rec.get("last_date")
    if not last: return False
    age = (TODAY - dt.date.fromisoformat(last)).days
    gap = (dt.date.fromisoformat(last) - dt.date.fromisoformat(rec["q_end"])).days
    return age <= 12 or (age <= 60 and gap > 80)   # keep trying until the new quarter lands

def fetch_one(item, mkt, cached, full):
    t = yf.Ticker(item["sym"])
    rec = dict(cached or {})
    rec.update(sym=item["sym"], name=item["name"], mkt=mkt)
    if item.get("ind"): rec["ind"] = item["ind"]
    nxt, last = dates_for(t)
    if last is None and rec.get("next_date") and rec["next_date"] < TODAY.isoformat():
        last = rec["next_date"]                      # remember a date that just passed
    rec["next_date"], rec["last_date"] = nxt, last or rec.get("last_date")
    if needs_refresh(rec, full):
        try:
            res = results_for(t, mkt)
            if res:                                  # keep old figures if every source failed
                for k in ("rev", "rev_prev", "prof", "prof_prev", "prof_label", "rev_yoy", "prof_yoy", "q_end", "src", "err", "q"):
                    rec.pop(k, None)
                rec.update(res)
            rec["fin_checked"] = TODAY.isoformat()
        except Exception as e:
            rec["err"] = str(e)[:120]
    return rec

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--auto", action="store_true", help="scheduled mode: once per day, retry when offline")
    ap.add_argument("--app", action="store_true", help="Nifty 500 only; write app-data.json for the Android app")
    a = ap.parse_args()
    if a.app:
        global APP, CACHE, UNIVERSE, NIFTY_URLS
        APP = True
        CACHE, UNIVERSE = HERE / "cache_app.json", HERE / "universe_app.json"
        NIFTY_URLS = tuple(x.replace("nifty100", "nifty500") for x in NIFTY_URLS)
    stamp = HERE / "last_ok.txt"
    if a.auto:
        if stamp.exists() and stamp.read_text().strip() == TODAY.isoformat():
            return
        try:
            requests.get("https://query2.finance.yahoo.com", headers=UA, timeout=10)
        except Exception:
            print(dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "offline, will retry"); return
        print(dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "auto run")

    u = load_universe()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() and not a.full else {}
    jobs = []
    for mkt in (("IN",) if a.app else ("IN", "US")):
        lst = u[mkt][: a.limit] if a.limit else u[mkt]
        jobs += [(i, mkt) for i in lst]
    print(f"Fetching {len(jobs)} stocks …")

    out, done = {}, 0
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fetch_one, i, m, cache.get(i["sym"]), a.full): i for i, m in jobs}
        for f in as_completed(futs):
            i = futs[f]
            try:
                out[i["sym"]] = f.result()
            except Exception as e:
                out[i["sym"]] = dict(cache.get(i["sym"], {}), sym=i["sym"], name=i["name"], err=str(e)[:120])
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(jobs)}")
    CACHE.write_text(json.dumps(out, indent=1))
    n_dates = sum(1 for r in out.values() if r.get("next_date"))
    if a.app:
        from app_data import build_app_data
        ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
        dest = HERE / "app-data.json"
        dest.write_text(json.dumps(build_app_data(list(out.values()), ist.strftime("%Y-%m-%dT%H:%M+05:30")),
                                   separators=(",", ":")))
        print(f"Done. {len(out)} stocks, {n_dates} with upcoming dates → {dest}")
    else:
        write_html(list(out.values()))
        print(f"Done. {len(out)} stocks, {n_dates} with upcoming dates → {OUT}")
    stamp.write_text(TODAY.isoformat())

def write_html(rows):
    tpl = (HERE / "template.html").read_text()
    payload = json.dumps({"generated": dt.datetime.now().strftime("%d %b %Y, %H:%M"),
                          "today": TODAY.isoformat(), "rows": rows})
    OUT.write_text(tpl.replace("/*__DATA__*/null", payload.replace("</", "<\\/")))
    art = HERE / "artifact_template.html"
    if art.exists():
        keep = ["sym","name","mkt","next_date","last_date","q_end","rev","rev_yoy","prof","prof_yoy","prof_label","src","rev_qoq","prof_qoq"]
        slim = json.dumps({"generated": dt.datetime.now().strftime("%d %b %Y, %H:%M IST"), "today": TODAY.isoformat(),
                           "rows": [{k: r.get(k) for k in keep} for r in rows]}, separators=(",", ":"))
        (HERE / "artifact.html").write_text(art.read_text().replace("/*__DATA__*/null", slim.replace("</", "<\\/")))

if __name__ == "__main__":
    main()
