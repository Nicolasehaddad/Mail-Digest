#!/usr/bin/env python3
"""Build and email the compact Market Dashboard digest."""

import argparse, csv, email.utils, html, json, math, os, re, smtplib, time
import urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Beirut")
UA = "Mozilla/5.0 NicolasMarketDigest/2.2"
MARKETS = {
    "xauusd": ("XAUUSD", "GC=F", "$", 1), "xagusd": ("XAGUSD", "SI=F", "$", 3),
    "eurusd": ("EURUSD", "EURUSD=X", "", 4), "dxy": ("DXY", "DX-Y.NYB", "", 2),
    "wti": ("WTI", "CL=F", "$", 2), "copper": ("Copper", "HG=F", "$", 3),
}
RATES = {"us2y": ("US 2Y yield", "DGS2"), "us10y": ("US 10Y yield", "DGS10"), "real10y": ("US 10Y real yield", "DFII10")}
FEEDS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Fed speeches", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("EIA", "https://www.eia.gov/rss/todayinenergy.xml"),
]
NEWS_RE = re.compile(r"gold|xau|silver|xag|dollar|dxy|fed|fomc|powell|waller|kashkari|yield|inflation|cpi|pce|payroll|jobs|oil|brent|wti|opec|eia|iran|hormuz|israel|lebanon|red sea|sanction|eurusd|\beur\b|eurozone|ecb|copper", re.I)


def fetch(url, timeout=14, attempts=2, accept="*/*"):
    last = ""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            if i + 1 < attempts:
                time.sleep(0.8 * (i + 1))
    raise RuntimeError(last)


def clean(value, limit=170):
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    text = text.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " "}))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def read_json(path):
    try:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def write_json(path, data):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def pct(value, prev):
    return 0.0 if not prev else (value - prev) / prev * 100


def fmt(row):
    if not row.get("available"):
        return "Unavailable"
    value = row["value"]; unit = row.get("unit", ""); d = row.get("decimals", 2)
    return f"{'$' if unit == '$' else ''}{value:,.{d}f}{'%' if unit == '%' else ''}"


def yahoo(label, symbol, unit, decimals):
    row = {"label": label, "symbol": symbol, "unit": unit, "decimals": decimals, "available": False}
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?range=2d&interval=5m"
    try:
        data = json.loads(fetch(url, accept="application/json,*/*"))["chart"]["result"][0]
        q = data["indicators"]["quote"][0]
        closes = [float(x) for x in q.get("close", []) if isinstance(x, (int, float))]
        highs = [float(x) for x in q.get("high", []) if isinstance(x, (int, float))]
        lows = [float(x) for x in q.get("low", []) if isinstance(x, (int, float))]
        if not closes:
            raise RuntimeError("no closes")
        value = closes[-1]
        prev = float(data.get("meta", {}).get("chartPreviousClose") or (closes[-2] if len(closes) > 1 else value))
        row.update(available=True, value=value, previous=prev, change=value - prev, pct=pct(value, prev), support=min(lows[-96:] or [value]), resistance=max(highs[-96:] or [value]))
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def fred_csv(series, label):
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + urllib.parse.quote(series)
    points = [(r.get("observation_date", ""), float(r[series])) for r in csv.DictReader(StringIO(fetch(url, accept="text/csv,*/*"))) if r.get(series) not in ("", ".")]
    if not points:
        raise RuntimeError("no valid observations")
    date, value = points[-1]; prev_date, prev = points[-2] if len(points) > 1 else points[-1]
    return {"label": label, "series": series, "unit": "%", "decimals": 2, "available": True, "date": date, "previous_date": prev_date, "value": value, "previous": prev, "change": value - prev, "source_name": "FRED CSV"}


def fred_api(series, label):
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY not set")
    start = (datetime.now(TZ).date() - timedelta(days=60)).isoformat()
    url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode({"series_id": series, "api_key": key, "file_type": "json", "observation_start": start})
    points = [(o["date"], float(o["value"])) for o in json.loads(fetch(url, accept="application/json,*/*")).get("observations", []) if o.get("value") not in (None, "", ".")]
    if not points:
        raise RuntimeError("no valid observations")
    date, value = points[-1]; prev_date, prev = points[-2] if len(points) > 1 else points[-1]
    return {"label": label, "series": series, "unit": "%", "decimals": 2, "available": True, "date": date, "previous_date": prev_date, "value": value, "previous": prev, "change": value - prev, "source_name": "FRED API"}


def trading_economics_real_yield(label):
    text = fetch("https://tradingeconomics.com/united-states/10-year-tips-yield")
    match = re.search(r"TIPS Yield .*? to\s+([0-9]+(?:\.[0-9]+)?)%\s+on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4}),\s+marking\s+a\s+([0-9]+(?:\.[0-9]+)?)\s+percentage points\s+(increase|decrease)", text, re.I | re.S)
    if match:
        value = float(match.group(1)); date = datetime.strptime(match.group(2), "%B %d, %Y").date().isoformat()
        change = float(match.group(3)) * (1 if match.group(4).lower() == "increase" else -1)
        prev = value - change
    else:
        match = re.search(r"10 Year TIPS Yield[^0-9]*([0-9]+(?:\.[0-9]+)?)", text, re.I | re.S)
        if not match:
            raise RuntimeError("Trading Economics value not found")
        value = float(match.group(1)); date = datetime.now(TZ).date().isoformat(); change = 0.0; prev = value
    return {"label": label, "series": "DFII10", "unit": "%", "decimals": 2, "available": True, "date": date, "value": value, "previous": prev, "change": change, "source_name": "Trading Economics fallback"}


def rate(label, series):
    errors = []
    for getter in (fred_api, fred_csv):
        try:
            return getter(series, label)
        except Exception as exc:
            errors.append(f"{getter.__name__}: {type(exc).__name__}: {exc}")
    if series == "DFII10":
        try:
            return trading_economics_real_yield(label)
        except Exception as exc:
            errors.append(f"trading_economics: {type(exc).__name__}: {exc}")
    return {"label": label, "series": series, "unit": "%", "decimals": 2, "available": False, "error": " | ".join(errors)}


def previous_slot(now):
    slots = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in (8, 13, 23)]
    past = [s for s in slots if s < now]
    return max(past) if past else (now - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)


def news(now):
    out, seen, since = [], set(), now.astimezone(timezone.utc) - timedelta(hours=30)
    for source, url in FEEDS:
        try:
            root = ET.fromstring(fetch(url, attempts=1, accept="application/rss+xml,application/xml,*/*"))
        except Exception:
            continue
        for item in root.findall(".//item")[:18]:
            title, desc = clean(item.findtext("title"), 135), clean(item.findtext("description"), 160)
            blob = title + " " + desc
            if not NEWS_RE.search(blob):
                continue
            try:
                dt = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
                if dt < since:
                    continue
            except Exception:
                dt = None
            key = re.sub(r"[^a-z0-9 ]", "", title.lower())[:70]
            if key in seen:
                continue
            seen.add(key); low = blob.lower(); assets = []
            if re.search(r"gold|xau|fed|yield|inflation|dollar|iran|hormuz|israel|lebanon|red sea|sanction", low): assets.append("XAUUSD")
            if re.search(r"silver|xag|copper|gold|fed|yield|dollar", low): assets.append("XAGUSD")
            if re.search(r"dollar|fed|yield|ecb|eurozone|eurusd|\beur\b", low): assets.append("EURUSD")
            if re.search(r"oil|brent|wti|opec|eia|hormuz|iran|tanker", low): assets.append("WTI")
            score = min(10, 3 + sum(w in low for w in ("fed", "yield", "inflation", "oil", "iran", "hormuz", "gold", "dollar", "ecb")))
            out.append({"source": source, "title": title, "desc": desc, "assets": assets or ["Macro"], "score": score, "why": "May affect USD/rates, safe-haven demand, energy inflation, or EUR repricing.", "new": bool(dt and dt.astimezone(TZ) > previous_slot(now))})
    return sorted(out, key=lambda x: x["score"], reverse=True)[:5]


def parse_event(date_s, time_s):
    if not date_s or not time_s or "tentative" in time_s.lower() or "day" in time_s.lower():
        return None
    try:
        base = datetime.strptime(date_s.strip(), "%m-%d-%Y")
        match = re.match(r"(\d{1,2}):(\d{2})(am|pm)", time_s.strip().lower().replace(" ", ""))
        if not match:
            return None
        hour, minute, ampm = match.groups()
        hour = int(hour) % 12 + (12 if ampm == "pm" else 0)
        return datetime(base.year, base.month, base.day, hour, int(minute), tzinfo=timezone.utc).astimezone(TZ)
    except Exception:
        return None


def calendar(now):
    sections = {"released": [], "next6": [], "next24": [], "next72": []}
    try:
        root = ET.fromstring(fetch("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", attempts=1, accept="application/xml,*/*"))
    except Exception:
        return sections
    important = re.compile(r"CPI|PCE|FOMC|Fed|Powell|Kashkari|Waller|Payroll|Unemployment|GDP|ISM|PMI|Inflation|Crude Oil Inventories|Petroleum", re.I)
    ps = previous_slot(now)
    for ev in root.findall(".//event"):
        title, ccy, impact = clean(ev.findtext("title"), 90), clean(ev.findtext("country"), 20), clean(ev.findtext("impact"), 20) or "Low"
        if impact not in {"High", "Medium"} and not important.search(title):
            continue
        if ccy not in {"USD", "EUR", "CNY"} and not important.search(title):
            continue
        dt = parse_event(ev.findtext("date") or "", ev.findtext("time") or "")
        if not dt:
            continue
        row = {"time": dt.strftime("%Y-%m-%d %H:%M"), "currency": ccy, "title": title, "impact": impact, "forecast": clean(ev.findtext("forecast"), 35) or "n/a", "previous": clean(ev.findtext("previous"), 35) or "n/a"}
        if ps <= dt < now: sections["released"].append(row)
        elif now <= dt <= now + timedelta(hours=6): sections["next6"].append(row)
        elif now < dt <= now + timedelta(hours=24): sections["next24"].append(row)
        elif now + timedelta(hours=24) < dt <= now + timedelta(hours=72): sections["next72"].append(row)
    return {k: sorted(v, key=lambda x: x["time"])[:8] for k, v in sections.items()}


def bias(score):
    if score >= 2.5: return "Strong bullish"
    if score >= 0.75: return "Bullish"
    if score <= -2.5: return "Strong bearish"
    if score <= -0.75: return "Bearish"
    return "Mixed"


def build_scores(markets, rates, ratio):
    rows, totals = [], {"xauusd": 0.0, "xagusd": 0.0, "eurusd": 0.0}
    def add(name, xau, xag, eur, read):
        rows.append((name, xau, xag, eur, read))
        totals["xauusd"] += xau[1]; totals["xagusd"] += xag[1]; totals["eurusd"] += eur[1]
    if markets["dxy"].get("available"):
        up = markets["dxy"]["pct"] < 0
        add("DXY", ("Bullish" if up else "Bearish", 1.5 if up else -1.5), ("Bullish" if up else "Bearish", 1.2 if up else -1.2), ("Bullish" if up else "Bearish", 1.5 if up else -1.5), f"DXY {markets['dxy']['pct']:+.2f}%")
    if rates["real10y"].get("available"):
        up = rates["real10y"]["change"] < 0
        add("US real yields", ("Bullish" if up else "Bearish", 1.5 if up else -1.5), ("Bullish" if up else "Bearish", 1.3 if up else -1.3), ("Neutral", 0), f"10Y TIPS {rates['real10y']['change']:+.2f} pts, latest {rates['real10y']['value']:.2f}%")
    else:
        add("US real yields", ("Unknown", 0), ("Unknown", 0), ("Neutral", 0), "Real-yield feed unavailable")
    for key, w in (("wti", 0.5), ("copper", 1.0)):
        if markets[key].get("available"):
            up = markets[key]["pct"] > 0
            if key == "wti":
                add("Oil / inflation", ("Mild bullish" if up else "Neutral", w if up else 0), ("Mild bullish" if up else "Neutral", 0.4 if up else 0), ("Bearish" if up else "Mild bullish", -0.5 if up else 0.3), f"WTI {markets[key]['pct']:+.2f}%")
            else:
                add("Copper / silver", ("Neutral", 0), ("Bullish" if up else "Bearish", w if up else -w), ("Neutral", 0), f"Copper {markets[key]['pct']:+.2f}%")
    if ratio.get("available"):
        up = ratio["change"] < 0
        add("Gold/silver ratio", ("Neutral", 0), ("Bullish" if up else "Bearish", 0.8 if up else -0.8), ("Neutral", 0), f"Ratio {ratio['change']:+.2f}")
    return rows, totals


def render_html(now, subject, analyst, cards, rows, totals, markets, rates, ratio, items, cal, quality, change_html):
    css = "body{margin:0!important;background:#07111F!important;color:#F8FAFC!important;font-family:Arial,Helvetica,sans-serif}.pre{display:none;max-height:0;overflow:hidden}.wrap{max-width:860px;margin:0 auto;padding:18px;color:#F8FAFC}.top,.section{border:1px solid #2D3A4E;background:#101927;border-radius:8px;padding:18px;margin:12px 0;color:#F8FAFC}.top{border-left:5px solid #60A5FA}.section.alt{background:#162235}h1{font-size:22px;line-height:1.25;margin:6px 0;color:#F8FAFC!important}h2{font-size:17px;margin:0 0 10px;color:#F8FAFC!important}h3{font-size:14px;margin:10px 0 6px;color:#F8FAFC!important}p{color:#E5E7EB!important;line-height:1.5}.muted,.read{color:#CBD5E1!important;font-size:12px}.badge{display:inline-block;border:1px solid #64748B;border-radius:999px;padding:4px 8px;font-size:12px;font-weight:bold}table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #2D3A4E;padding:8px;text-align:left;font-size:13px;vertical-align:top;color:#E5E7EB!important}th{color:#CBD5E1!important}.asset{width:33%;vertical-align:top;border:1px solid #2D3A4E;background:#101927;border-radius:8px;padding:12px}.price{font-size:24px;font-weight:bold;margin:10px 0;color:#F8FAFC}.bar{height:7px;background:#253348;border-radius:999px;overflow:hidden;margin-top:8px}.bar span{display:block;height:7px;border-radius:999px}.news,.event{border-top:1px solid #2D3A4E;padding:9px 0;color:#E5E7EB;line-height:1.4}.pie{width:112px;height:112px;border-radius:50%;border:1px solid #2D3A4E}.disclaimer{font-size:12px;color:#CBD5E1!important}@media(max-width:680px){.wrap{padding:10px}.asset{display:block;width:auto;margin:8px 0}th,td{font-size:12px;padding:6px}}"
    def badge(text):
        color = "#86EFAC" if "bullish" in text.lower() else "#FDA4AF" if "bearish" in text.lower() else "#FBBF24"
        return f"<span class='badge' style='border-color:{color};color:{color}'>{html.escape(text)}</span>"
    def asset(key):
        label, view, score, driver, invalid = cards[key]; row = markets[key]
        width = max(8, min(100, abs(score) / 5 * 100)); color = "#86EFAC" if score >= 0 else "#FDA4AF"
        return f"<td class='asset'><h3>{label}</h3>{badge(view)}<div class='price'>{fmt(row)}</div><div class='bar'><span style='width:{width:.0f}%;background:{color}'></span></div><p><b>Main driver:</b> {driver}</p><p><b>Invalidation:</b> {invalid}</p></td>"
    score_html = "".join(f"<td><b>{k.upper()}</b><div>{v:+.1f}</div><div class='bar'><span style='width:{max(8,min(100,abs(v)/5*100)):.0f}%;background:{'#86EFAC' if v>=0 else '#FDA4AF'}'></span></div></td>" for k, v in totals.items())
    matrix = "".join(f"<tr><td><b>{html.escape(r[0])}</b><div class='read'>{html.escape(r[4])}</div></td><td>{badge(r[1][0])}</td><td>{badge(r[2][0])}</td><td>{badge(r[3][0])}</td></tr>" for r in rows)
    snap_rows = []
    for group, row in [("m", markets["xauusd"]), ("m", markets["xagusd"]), ("m", markets["eurusd"]), ("m", markets["dxy"]), ("r", rates["us10y"]), ("r", rates["real10y"]), ("m", markets["wti"]), ("m", markets["copper"])]:
        move = "Unavailable"
        if row.get("available"):
            move = f"{row.get('pct', 0):+.2f}%" if group == "m" else f"{row.get('change', 0):+.2f} pts"
        snap_rows.append(f"<tr><td><b>{html.escape(row['label'])}</b></td><td>{fmt(row)}</td><td>{move}</td><td>{'OK' if row.get('available') else 'Missing'}</td></tr>")
    buckets = {"Macro/Rates": 0, "Energy": 0, "Geopolitics": 0, "FX": 0}
    for item in items:
        blob = (item["title"] + " " + item["desc"]).lower()
        buckets["Macro/Rates"] += bool(re.search(r"fed|yield|inflation|cpi|pce|jobs", blob))
        buckets["Energy"] += bool(re.search(r"oil|wti|brent|opec|eia", blob))
        buckets["Geopolitics"] += bool(re.search(r"iran|hormuz|israel|lebanon|red sea|war|attack", blob))
        buckets["FX"] += bool(re.search(r"dollar|ecb|euro|eurusd", blob))
    total = sum(buckets.values()) or 1; cursor = 0; colors = ["#60A5FA", "#FBBF24", "#FDA4AF", "#86EFAC"]; segs = []; legend = []
    for i, (name, count) in enumerate(buckets.items()):
        share = count / total * 100; segs.append(f"{colors[i]} {cursor:.1f}% {cursor + share:.1f}%"); cursor += share
        legend.append(f"<div><span style='display:inline-block;width:10px;height:10px;background:{colors[i]};border-radius:50%;margin-right:6px'></span>{name}: {count}</div>")
    news_html = "".join(f"<div class='news'><b>{html.escape(n['title'])}</b><div>{html.escape(n['why'])}</div><div class='muted'>Impact {n['score']}/10 | Assets: {html.escape(', '.join(n['assets']))}</div></div>" for n in items) or "<p>No major matching headline found.</p>"
    cal_html = "".join(f"<h3>{label}</h3>" + ("".join(f"<div class='event'>{e['time']} Asia/Beirut | {e['currency']} | {html.escape(e['title'])} | {e['impact']} | Cons. {html.escape(e['forecast'])} | Prev. {html.escape(e['previous'])}</div>" for e in cal[key]) if cal[key] else "<div class='event muted'>None found.</div>") for key, label in [("released", "Released since last digest"), ("next6", "Upcoming next 6h"), ("next24", "Upcoming next 24h"), ("next72", "Upcoming 24-72h")])
    quality_html = "".join(f"<li style='color:#E5E7EB'>{html.escape(q)}</li>" for q in quality)
    return f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><style>{css}</style></head><body style='margin:0;background:#07111F;color:#F8FAFC'><div class='pre'>{html.escape(subject)}</div><div class='wrap'><div class='top'><div class='muted'>Market Intelligence Digest</div><h1>{html.escape(subject)}</h1><div class='muted'>{now:%Y-%m-%d %H:%M} Asia/Beirut</div></div><div class='section'><h2>Analyst Brief</h2><p>{html.escape(analyst)}</p></div><div class='section alt'><h2>Clear Bias Line</h2><p>XAUUSD {cards['xauusd'][1].lower()} | XAGUSD {cards['xagusd'][1].lower()} | EURUSD {cards['eurusd'][1].lower()}</p></div><div class='section'><h2>Key Asset Read</h2><table role='presentation'><tr>{asset('xauusd')}{asset('xagusd')}{asset('eurusd')}</tr></table></div><div class='section alt'><h2>What Changed Since Last Digest</h2><table><tr><th>Input</th><th>Move</th><th>Asset affected</th></tr>{change_html}</table></div><div class='section'><h2>Driver Bars</h2><table><tr>{score_html}</tr></table><table><tr><th>Driver</th><th>XAUUSD</th><th>XAGUSD</th><th>EURUSD</th></tr>{matrix}</table></div><div class='section alt'><h2>Live Market Snapshot</h2><table><tr><th>Asset/Driver</th><th>Now</th><th>Move</th><th>Status</th></tr>{''.join(snap_rows)}</table></div><div class='section'><h2>News Impact Mix</h2><div style='display:flex;gap:18px;align-items:center'><div class='pie' style='background:conic-gradient({', '.join(segs)})'></div><div>{''.join(legend)}</div></div></div><div class='section alt'><h2>Top News by Market Impact</h2>{news_html}</div><div class='section'><h2>Calendar Risk</h2>{cal_html}</div><div class='section alt'><h2>Data Quality</h2><ul>{quality_html}</ul></div><div class='section'><h2>Disclaimer</h2><p class='disclaimer'>Market context only; not financial advice. Directional reads are hypotheses that require live price, liquidity, risk, and invalidation review before action.</p></div></div></body></html>"


def make_digest(args, now):
    markets = {k: yahoo(*v) for k, v in MARKETS.items()}
    rates = {k: rate(*v) for k, v in RATES.items()}
    if markets["xauusd"].get("available") and markets["xagusd"].get("available"):
        value = markets["xauusd"]["value"] / markets["xagusd"]["value"]; prev = markets["xauusd"]["previous"] / markets["xagusd"]["previous"]
        ratio = {"label": "Gold/silver ratio", "available": True, "value": value, "previous": prev, "change": value - prev, "unit": "", "decimals": 2}
    else:
        ratio = {"label": "Gold/silver ratio", "available": False}
    rows, totals = build_scores(markets, rates, ratio)
    cards = {"xauusd": ("XAUUSD", bias(totals["xauusd"]), totals["xauusd"], "DXY and real-yield pressure", "DXY and real yields rise together"),
             "xagusd": ("XAGUSD", bias(totals["xagusd"]), totals["xagusd"], "metals macro plus copper/ratio confirmation", "copper rolls over and the gold/silver ratio rises"),
             "eurusd": ("EURUSD", bias(totals["eurusd"]), totals["eurusd"], "DXY and US front-end yield pressure", "DXY rebounds while US 2Y rises faster")}
    previous, items, cal = read_json(args.snapshot_file), news(now), calendar(now)
    def delta(group, key):
        row = ratio if group == "ratio" else (markets if group == "markets" else rates)[key]
        if not row.get("available"):
            return "Unavailable"
        prev = previous.get(group, {}).get(key, {}).get("value") if group != "ratio" else previous.get("ratio", {}).get("value")
        move = row["value"] - prev if isinstance(prev, (int, float)) else row.get("change", 0)
        return f"{move:+.{row.get('decimals', 2)}f}{' pts' if row.get('unit') == '%' else ''}"
    specs = [("DXY", "markets", "dxy", "XAUUSD, XAGUSD, EURUSD"), ("US 10Y real yield", "rates", "real10y", "XAUUSD, XAGUSD"), ("WTI", "markets", "wti", "XAUUSD, EURUSD, inflation"), ("Copper", "markets", "copper", "XAGUSD"), ("Gold/silver ratio", "ratio", "ratio", "XAGUSD")]
    change_html = "".join(f"<tr><td>{a}</td><td><b>{delta(g, k)}</b></td><td>{assets}</td></tr>" for a, g, k, assets in specs)
    change_text = "; ".join(f"{a}: {delta(g, k)}" for a, g, k, _ in specs)
    next_cat = next((f"{e['currency']} {e['title']}" for sec in ("next6", "next24", "next72") for e in cal[sec][:1]), "Next USD/Fed catalyst")
    real_note = f"Real-yield confirmation is available from {rates['real10y'].get('source_name', 'source')}." if rates["real10y"].get("available") else "Confidence is reduced because real yield is unavailable."
    analyst = f"Metals are {cards['xauusd'][1].lower()} while EURUSD is {cards['eurusd'][1].lower()}. Since the previous digest: {change_text}. XAUUSD is driven by {cards['xauusd'][3]}; XAGUSD is filtered through copper and the gold/silver ratio; EURUSD is filtered through DXY and US front-end yields. {real_note} The next major catalyst is {next_cat}. The view is wrong if {cards['xauusd'][4]} for metals, or if {cards['eurusd'][4]} for EURUSD. Treat this as a concise market map, not a trade signal."
    quality = [f"10Y real yield available from {rates['real10y'].get('source_name','source')}: {rates['real10y']['value']:.2f}% on {rates['real10y'].get('date','latest')}." if rates["real10y"].get("available") else f"Real-yield feed unavailable: {rates['real10y'].get('error','source issue')}.", "XAUUSD and XAGUSD use Yahoo futures proxies GC=F and SI=F.", f"News filter returned {len(items)} market-impact items.", f"Calendar parser returned {sum(len(v) for v in cal.values())} relevant Asia/Beirut events."]
    subject = f"Market Dashboard | XAU: {cards['xauusd'][1]} | XAG: {cards['xagusd'][1]} | EURUSD: {cards['eurusd'][1]}"
    html_body = render_html(now, subject, analyst, cards, rows, totals, markets, rates, ratio, items, cal, quality, change_html)
    text_body = f"Market Dashboard Digest - {now:%Y-%m-%d %H:%M} Asia/Beirut\n\nAnalyst Brief\n{analyst}\n\nData Quality\n" + "\n".join(f"- {q}" for q in quality) + "\n\nMarket context only; not financial advice.\n"
    return {"subject": subject, "html_body": html_body, "text_body": text_body, "markets": markets, "rates": rates, "ratio": ratio, "cards": cards, "driver_scores": totals, "news": items, "calendar": cal, "quality": quality}


def select_slot(args, now):
    if args.force_send or os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return "manual-" + now.strftime("%Y-%m-%d-%H%M"), "manual/test run"
    sent = set(read_json(args.state_file).get("sent_slots", [])); candidates = []
    for hour in [int(x) for x in args.scheduled_slots.split(",") if x.strip().isdigit()]:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0); delta = now - target
        if timedelta(0) <= delta <= timedelta(minutes=args.send_window_minutes):
            candidates.append(target)
    if not candidates:
        return "", f"{now:%Y-%m-%d %H:%M} Asia/Beirut is outside the configured send windows"
    slot = max(candidates).strftime("%Y-%m-%d-%H")
    return ("", f"slot {slot} already sent") if slot in sent else (slot, f"scheduled Beirut slot {slot}")


def send_email(args, subject, text_body, html_body):
    recipients = [x.strip() for x in args.email_to.split(",") if x.strip()]
    if not recipients: raise RuntimeError("EMAIL_TO is empty")
    if not args.smtp_username or not args.smtp_password: raise RuntimeError("SMTP credentials missing")
    msg = EmailMessage(); msg["From"] = args.smtp_from or args.smtp_username; msg["To"] = ", ".join(recipients); msg["Subject"] = subject
    msg.set_content(text_body); msg.add_alternative(html_body, subtype="html")
    if args.smtp_port == 465 and not args.smtp_starttls:
        with smtplib.SMTP_SSL(args.smtp_host, args.smtp_port, timeout=30) as smtp:
            smtp.login(args.smtp_username, args.smtp_password); smtp.send_message(msg)
    else:
        with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=30) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.ehlo(); smtp.login(args.smtp_username, args.smtp_password); smtp.send_message(msg)
    return f"Sent dashboard digest to {', '.join(recipients)}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, default=Path("firm/outputs/market-dashboard-digests/latest"))
    p.add_argument("--state-file", type=Path, default=Path(".digest-state/dashboard-send-slots.json"))
    p.add_argument("--snapshot-file", type=Path, default=Path(".digest-state/last-dashboard-snapshot.json"))
    p.add_argument("--scheduled-slots", default="8,13,23"); p.add_argument("--send-window-minutes", type=int, default=55)
    p.add_argument("--send-email", action="store_true"); p.add_argument("--force-send", action="store_true")
    p.add_argument("--email-to", default=os.getenv("EMAIL_TO", "")); p.add_argument("--smtp-host", default=os.getenv("SMTP_HOST", "smtp.gmail.com"))
    p.add_argument("--smtp-port", type=int, default=int(os.getenv("SMTP_PORT") or 465)); p.add_argument("--smtp-username", default=os.getenv("SMTP_USERNAME", ""))
    p.add_argument("--smtp-password", default=os.getenv("SMTP_PASSWORD", "")); p.add_argument("--smtp-from", default=os.getenv("SMTP_FROM", ""))
    p.add_argument("--smtp-starttls", action="store_true", default=os.getenv("SMTP_STARTTLS", "").lower() in {"1", "true", "yes"})
    args = p.parse_args(); now = datetime.now(TZ); slot = ""
    if args.send_email:
        slot, reason = select_slot(args, now); print("Email send decision:", reason)
        if not slot:
            return 0
    digest = make_digest(args, now); args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "market-dashboard.html").write_text(digest["html_body"], encoding="utf-8")
    (args.output_dir / "market-dashboard.txt").write_text(digest["text_body"], encoding="utf-8")
    write_json(args.output_dir / "market-dashboard.json", {"generated_at": now.isoformat(), **{k: digest[k] for k in ("markets", "rates", "ratio", "cards", "driver_scores", "news", "calendar", "quality")}})
    if args.send_email:
        print(send_email(args, digest["subject"], digest["text_body"], digest["html_body"]))
        state = read_json(args.state_file)
        write_json(args.state_file, {"sent_slots": list(dict.fromkeys([*state.get("sent_slots", []), slot]))[-90:], "updated_at": now.isoformat()})
        write_json(args.snapshot_file, {"generated_at": now.isoformat(), "markets": digest["markets"], "rates": digest["rates"], "ratio": digest["ratio"], "driver_scores": digest["driver_scores"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
