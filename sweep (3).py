#!/usr/bin/env python3
"""
Insider Buy Portfolio - daily sweep engine.

What it does, in order:
  1. Reads SEC EDGAR's daily index over a rolling window (LOOKBACK_DAYS) so late
     filers are never missed.
  2. Keeps Form 4 / 4-A filings, fetches each ownership XML, and parses
     open-market PURCHASES (transaction code P, shares acquired).
  3. Screens: dollar floor (DOLLAR_FLOOR), excludes funds / SPACs / shells.
     NOTE: there is no hard delta-ownership gate - small delta-own just scores lower.
  4. Classifies each buyer opportunistic vs routine (Cohen-Malloy-Pomorski:
     routine = bought the same calendar month for 3+ consecutive years).
  5. Flags clusters (2+ insiders on one issuer) and CEO/CFO buys.
  6. Scores each name 0-100 (conviction score) and ranks by it.
  7. Dedupes against state/seen_accessions.json so repeats aren't re-counted.
  8. Writes the dashboard to docs/index.html (GitHub Pages serves it).

The Dorsey Wright rating and Entry price are intentionally LEFT BLANK in the
dashboard - those are the manual columns you fill in. Only DW = 5 names are buys.

Every knob is an environment variable so you can tune from the workflow file
without touching this code.
"""

import os
import re
import sys
import json
import time
import html
import math
import datetime as dt
from xml.etree import ElementTree as ET

import requests

# --------------------------------------------------------------------------- #
#  Settings (override any of these in the workflow's env: block)
# --------------------------------------------------------------------------- #
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
DOLLAR_FLOOR   = float(os.environ.get("DOLLAR_FLOOR", "500000"))   # $500K
LOOKBACK_DAYS  = int(os.environ.get("LOOKBACK_DAYS", "4"))         # rolling window
ROUTINE_YEARS  = int(os.environ.get("ROUTINE_YEARS", "3"))         # consecutive yrs
REQUEST_PAUSE  = float(os.environ.get("REQUEST_PAUSE", "0.20"))    # politeness delay

STATE_PATH = os.environ.get("STATE_PATH", "state/seen_accessions.json")
OUT_PATH   = os.environ.get("OUT_PATH",   "docs/index.html")

# Issuer-name hints that mark a fund / SPAC / shell we don't want.
EXCLUDE_NAME_HINTS = (
    "fund", "trust ", " trust", "spac", "acquisition corp", "acquisition co",
    "capital corp", "bdc", "etf", "holdings acquisition", "blank check",
)

SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT or "insider-sweep contact@example.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}
DATA_HEADERS = dict(SEC_HEADERS, Host="data.sec.gov")

NS = {"o": "http://www.sec.gov/edgar/ownership"}


# --------------------------------------------------------------------------- #
#  Small HTTP helper with retry + politeness
# --------------------------------------------------------------------------- #
def fetch(url, headers, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                time.sleep(REQUEST_PAUSE)
                return r
            if r.status_code in (403, 429):
                time.sleep(1.5 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.0 * (i + 1))
    return None


# --------------------------------------------------------------------------- #
#  Step 1 - daily index -> list of Form 4 filings
# --------------------------------------------------------------------------- #
def index_url(day):
    q = (day.month - 1) // 3 + 1
    return (f"https://www.sec.gov/Archives/edgar/daily-index/"
            f"{day.year}/QTR{q}/form.{day:%Y%m%d}.idx")


def form4_filings(day):
    """Return [(cik, accession, issuer_name), ...] for Form 4 / 4-A on `day`."""
    r = fetch(index_url(day), SEC_HEADERS)
    out = []
    if not r:
        return out
    for line in r.text.splitlines():
        if not (line.startswith("4 ") or line.startswith("4/A")):
            continue
        # Fixed-ish columns: Form Type | Company | CIK | Date | File Name
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form, company, cik, _date, path = parts[0], parts[1], parts[2], parts[3], parts[4]
        if form not in ("4", "4/A"):
            continue
        m = re.search(r"/(\d{10}-\d{2}-\d{6})\.txt", path)
        if not m:
            continue
        out.append((cik.strip(), m.group(1), company.strip()))
    return out


def ownership_xml_url(cik, accession):
    acc_nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}"
    r = fetch(f"{base}/index.json", SEC_HEADERS)
    if not r:
        return None
    try:
        items = r.json()["directory"]["item"]
    except (ValueError, KeyError):
        return None
    # Prefer the primary ownership document (an .xml that is not the R-rendered one).
    for it in items:
        name = it.get("name", "")
        if name.endswith(".xml") and "ownership" in name.lower():
            return f"{base}/{name}"
    for it in items:
        name = it.get("name", "")
        if name.endswith(".xml") and not name.lower().startswith("r"):
            return f"{base}/{name}"
    return None


# --------------------------------------------------------------------------- #
#  Step 2 - parse one Form 4 ownership XML into purchase rows
# --------------------------------------------------------------------------- #
def text(node, path):
    el = node.find(path, NS) if node is not None else None
    return el.text.strip() if (el is not None and el.text) else ""


def parse_form4(xml_bytes, cik, accession, issuer_name):
    """Return a list of purchase dicts (one per code-P acquired lot)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    # strip namespace for resilient lookups
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    ticker = (root.findtext(".//issuer/issuerTradingSymbol") or "").strip().upper()
    issuer = (root.findtext(".//issuer/issuerName") or issuer_name).strip()

    owner = root.find(".//reportingOwner")
    owner_name = (owner.findtext(".//rptOwnerName") if owner is not None else "") or ""
    rel = owner.find(".//reportingOwnerRelationship") if owner is not None else None
    is_dir = (rel.findtext("isDirector") if rel is not None else "") in ("1", "true")
    is_off = (rel.findtext("isOfficer") if rel is not None else "") in ("1", "true")
    is_ten = (rel.findtext("isTenPercentOwner") if rel is not None else "") in ("1", "true")
    title  = (rel.findtext("officerTitle") if rel is not None else "") or ""
    owner_cik = (owner.findtext(".//rptOwnerCik") if owner is not None else "") or ""

    rows = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = (tx.findtext(".//transactionCoding/transactionCode") or "").strip()
        ad   = (tx.findtext(".//transactionAcquiredDisposedCode/value") or "").strip()
        if code != "P" or ad != "A":          # open-market purchase, acquired
            continue
        try:
            shares = float(tx.findtext(".//transactionShares/value") or 0)
            price  = float(tx.findtext(".//transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        if shares <= 0 or price <= 0:
            continue
        post = tx.findtext(".//postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        try:
            owned_after = float(post or 0)
        except ValueError:
            owned_after = 0.0
        owned_before = max(owned_after - shares, 0.0)
        d_own = (shares / owned_before * 100.0) if owned_before > 0 else 999.0
        tdate = (tx.findtext(".//transactionDate/value") or "").strip()

        rows.append({
            "ticker": ticker, "issuer": issuer, "owner": owner_name.strip(),
            "owner_cik": owner_cik.strip(), "title": title.strip(),
            "is_dir": is_dir, "is_off": is_off, "is_ten": is_ten,
            "shares": shares, "price": price, "value": shares * price,
            "owned_after": owned_after, "d_own": d_own,
            "trade_date": tdate, "cik": cik, "accession": accession,
        })
    return rows


# --------------------------------------------------------------------------- #
#  Step 3 - screen
# --------------------------------------------------------------------------- #
def passes_screen(row):
    if row["value"] < DOLLAR_FLOOR:
        return False
    name = (row["issuer"] or "").lower()
    if any(h in name for h in EXCLUDE_NAME_HINTS):
        return False
    return True


# --------------------------------------------------------------------------- #
#  Step 4 - opportunistic vs routine (EDGAR submission history)
# --------------------------------------------------------------------------- #
def classify(owner_cik, month):
    """routine if the owner filed a purchase in `month` for ROUTINE_YEARS+
    consecutive years; opportunistic otherwise; 'unknown' if history missing."""
    if not owner_cik:
        return "unknown"
    cik10 = owner_cik.zfill(10)
    r = fetch(f"https://data.sec.gov/submissions/CIK{cik10}.json", DATA_HEADERS)
    if not r:
        return "unknown"
    try:
        recent = r.json().get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        dates  = recent.get("filingDate", [])
    except ValueError:
        return "unknown"
    years = set()
    for f, d in zip(forms, dates):
        if f in ("4", "4/A") and len(d) >= 7:
            y, m = int(d[:4]), int(d[5:7])
            if m == month:
                years.add(y)
    if not years:
        return "unknown"
    this_year = dt.date.today().year
    streak = 0
    for y in range(this_year, this_year - 10, -1):
        if y in years:
            streak += 1
        elif streak:
            break
    return "routine" if streak >= ROUTINE_YEARS else "opportunistic"


# --------------------------------------------------------------------------- #
#  Step 5/6 - conviction score (0-100)
# --------------------------------------------------------------------------- #
def role_points(row):
    t = (row["title"] or "").lower()
    if "cfo" in t or "chief financial" in t:
        return 30
    if "ceo" in t or "chief executive" in t or "president" in t:
        return 25
    if row["is_off"]:
        return 15
    if row["is_dir"]:
        return 10
    if row["is_ten"]:
        return 3
    return 5


def size_points(value):
    if value >= 5_000_000:
        return 15
    if value >= 1_000_000:
        return 12
    if value >= 500_000:
        return 9
    if value >= 100_000:
        return 6
    return 3


def down_points(d_own):
    # smooth saturating curve: +5%->~2.8, +15%->~6.3, +30%->~8.6, capped at 10
    return round(min(10.0, 10.0 * (1.0 - math.exp(-d_own / 15.0))), 1)


def conviction(row, classification, cluster_size):
    pts  = role_points(row)
    pts += {"opportunistic": 25, "unknown": 12, "routine": 0}[classification]
    pts += 15 if cluster_size >= 3 else (8 if cluster_size == 2 else 0)
    pts += size_points(row["value"])
    pts += down_points(row["d_own"])
    # contrarian bonus intentionally 0 - it conflicts with the DW=5 momentum-only
    # philosophy; left here as a labelled knob to revisit later.
    return round(min(100.0, pts), 1)


# --------------------------------------------------------------------------- #
#  Dashboard
# --------------------------------------------------------------------------- #
def flags_for(row, classification, cluster_size):
    f = []
    t = (row["title"] or "").lower()
    if "ceo" in t or "chief executive" in t:
        f.append("CEO")
    if "cfo" in t or "chief financial" in t:
        f.append("CFO")
    if cluster_size >= 2:
        f.append(f"CLUSTER x{cluster_size}")
    if classification == "routine":
        f.append("ROUTINE")
    if classification == "unknown":
        f.append("CHECK HISTORY")
    if row["price"] < 5:
        f.append("MICROCAP?")
    return f


def render(rows):
    today = dt.date.today().isoformat()
    head = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Insider Buy Sweep</title><style>
:root{--navy:#0f2741;--line:#d6dde5;--mute:#5d6b78;--blue:#2f6fb0;--good:#1d6b4f}
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#10202e;background:#f7f8fa}
header{background:#0f2741;color:#fff;padding:24px 20px}
h1{margin:0;font-size:22px}.asof{color:#cdddec;font-size:14px;margin-top:4px}
.wrap{max-width:1180px;margin:0 auto;padding:16px 20px 60px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;border:1px solid var(--line)}
th{background:#0f2741;color:#fff;text-align:left;padding:9px;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
td{padding:9px;border-top:1px solid var(--line);vertical-align:top}
tr:nth-child(even){background:#fafbfc}.tkr{font-weight:700;color:var(--navy)}
.co{color:var(--mute);font-size:12px}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.score{font-weight:700;color:var(--blue)}.up{color:var(--good)}
.flag{display:inline-block;background:#eef1f4;color:#3a4a59;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;margin:1px}
.man{background:#f0f3f7;color:#aeb8c2;text-align:center}
.note{background:#fff;border-left:3px solid var(--blue);padding:12px 14px;margin:14px 0;font-size:13px}
</style></head><body>
<header><h1>Insider Buy Portfolio &mdash; Daily Sweep</h1>
<div class="asof">As of {today} &middot; ranked by conviction</div></header><div class="wrap">
<div class="note"><b>DW</b> and <b>Entry $</b> are blank on purpose &mdash; pull the Dorsey Wright
ratings, fill DW, and keep only the <b>DW&nbsp;5</b> names. Conviction only ranks the list.</div>
<table><thead><tr><th>#</th><th>Score</th><th>Ticker</th><th>Insider &middot; Role</th>
<th class="num">&Delta;Own</th><th class="num">Value</th><th>Trade</th><th>Flags</th>
<th>DW</th><th>Entry&nbsp;$</th></tr></thead><tbody>
""".replace("{today}", today)

    body = []
    if not rows:
        body.append('<tr><td colspan="10" style="padding:24px;text-align:center;color:#5d6b78">'
                     'No new qualifying purchases in this window.</td></tr>')
    for i, r in enumerate(rows, 1):
        fl = "".join(f'<span class="flag">{html.escape(x)}</span>' for x in r["flags"])
        dpct = "New" if r["d_own"] >= 999 else f'+{r["d_own"]:.0f}%'
        body.append(
            f'<tr><td>{i}</td><td class="score">{r["score"]:.0f}</td>'
            f'<td><span class="tkr">{html.escape(r["ticker"] or "?")}</span><br>'
            f'<span class="co">{html.escape(r["issuer"][:32])}</span></td>'
            f'<td>{html.escape(r["owner"])}<br><span class="co">{html.escape(r["title"][:34])}</span></td>'
            f'<td class="num up">{dpct}</td>'
            f'<td class="num">${r["value"]:,.0f}</td>'
            f'<td>{html.escape(r["trade_date"])}</td>'
            f'<td>{fl}</td><td class="man">&mdash;</td><td class="man">&mdash;</td></tr>'
        )
    tail = ("</tbody></table><p style='color:#5d6b78;font-size:11px;margin-top:16px'>"
            "Source: SEC Form 4 open-market purchases via EDGAR. Conviction scores are "
            "estimates, not advice. Verify each filing before trading.</p></div></body></html>")
    return head + "\n".join(body) + tail


# --------------------------------------------------------------------------- #
#  State
# --------------------------------------------------------------------------- #
def load_seen():
    try:
        with open(STATE_PATH) as fh:
            return set(json.load(fh))
    except (FileNotFoundError, ValueError):
        return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        json.dump(sorted(seen), fh)


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main():
    if not SEC_USER_AGENT:
        print("WARNING: SEC_USER_AGENT is not set. SEC may block requests. "
              "Set it as a repository Variable (Name -> you, Email).")

    today = dt.date.today()
    days = [today - dt.timedelta(days=i) for i in range(LOOKBACK_DAYS)]
    seen = load_seen()

    raw = []
    for day in days:
        filings = form4_filings(day)
        print(f"{day}: {len(filings)} Form 4 filings")
        for cik, accession, issuer_name in filings:
            if accession in seen:
                continue
            xml_url = ownership_xml_url(cik, accession)
            if not xml_url:
                continue
            resp = fetch(xml_url, SEC_HEADERS)
            if not resp:
                continue
            for row in parse_form4(resp.content, cik, accession, issuer_name):
                if passes_screen(row):
                    raw.append(row)
            seen.add(accession)

    # cluster sizes: distinct insiders per issuer in this batch
    by_issuer = {}
    for r in raw:
        by_issuer.setdefault(r["ticker"] or r["issuer"], set()).add(r["owner"])
    cluster = {k: len(v) for k, v in by_issuer.items()}

    results = []
    for r in raw:
        key = r["ticker"] or r["issuer"]
        csize = cluster.get(key, 1)
        month = int(r["trade_date"][5:7]) if len(r["trade_date"]) >= 7 else today.month
        cls = classify(r["owner_cik"], month)
        r["score"] = conviction(r, cls, csize)
        r["flags"] = flags_for(r, cls, csize)
        results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as fh:
        fh.write(render(results))
    save_seen(seen)
    print(f"Wrote {OUT_PATH} with {len(results)} ranked candidates.")


if __name__ == "__main__":
    sys.exit(main())
