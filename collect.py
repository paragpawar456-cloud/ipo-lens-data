#!/usr/bin/env python3
"""
Fetches open, upcoming and recent IPOs with live GMP and subscription, and writes a normalised
ipos.json for the IPO Lens app. Keeps its own day-wise GMP history.

Two providers are supported; the first one with a credential set is used:
  APIFY_TOKEN      India IPO Tracker actor on Apify (reads InvestorGain's GMP report).
                   Free Apify plan includes $5/month; one daily run of ~40 rows costs about $0.12.
  IPOGURU_API_KEY  IPO Guru API (free plan: 10 requests/day, non-commercial).

Usage: APIFY_TOKEN=... python3 collect.py [path/to/ipos.json]
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

IPOGURU_API = "https://www.ipoguru.in/api/v2/ipos?months=3"
APIFY_API = "https://api.apify.com/v2/acts/data_daemon~india-ipo-tracker/run-sync-get-dataset-items?format=json&clean=true"
SCHEMA_VERSION = 1
KEEP_LISTED_DAYS = 45
MAX_HISTORY_DAYS = 30
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def num(v):
    """Parse numbers that may arrive as strings such as '₹1,234.5 Cr' or '7.5'. None if absent."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def issue_size_cr(v):
    n = num(v)
    if n is None:
        return None
    # Some feeds give rupees rather than crore.
    return round(n / 1e7, 2) if n > 1e6 else n


def day(v):
    if not v:
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        return None


def symbol_from(item):
    for key in ("nse_symbol", "symbol", "bse_scrip_code"):
        if item.get(key):
            return str(item[key]).upper()
    slug = re.sub(r"-ipo$", "", item.get("slug", ""))
    words = [w for w in slug.split("-") if w and w not in ("limited", "ltd", "india", "the")]
    if not words:
        return "IPO"
    first = words[0] if len(words[0]) >= 4 or len(words) == 1 else "".join(words[:2])
    return first.upper()[:10]


def ipo_type(v):
    return "SME" if v and "sme" in str(v).lower() else "MAINBOARD"


def status(v):
    s = (v or "").strip().lower()
    for k in ("open", "upcoming", "closed", "listed"):
        if k in s:
            return k.upper()
    return None


def fetch_ipoguru(key):
    req = urllib.request.Request(IPOGURU_API, headers={
        "X-API-KEY": key,
        "Accept": "application/json",
        "User-Agent": "IPOLens-collector/1.0",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.load(r)
    if not body.get("success", True) or "data" not in body:
        raise RuntimeError(f"Unexpected API response: {str(body)[:300]}")
    return body["data"]


def fetch_apify(token):
    payload = json.dumps({"reports": ["gmp"], "category": "all", "maxItems": 50}).encode()
    req = urllib.request.Request(APIFY_API, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "IPOLens-collector/1.0",
    })
    with urllib.request.urlopen(req, timeout=300) as r:
        rows = json.load(r)
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected Apify response: {str(rows)[:300]}")
    return [row for row in rows if row.get("reportType", "gmp") == "gmp" and row.get("slug")]


def normalise_apify(row, today):
    """Map an InvestorGain GMP row (via Apify) to the same shape as the IPO Guru mapping."""
    price = num(row.get("issuePrice"))
    gmp = num(row.get("gmp"))
    return normalise({
        "slug": row["slug"],
        "display_name": row.get("ipoName"),
        "type": row.get("category"),
        "status": row.get("statusLabel") or row.get("status"),
        "web_url": row.get("url"),
        "open_date": row.get("openDate"),
        "close_date": row.get("closeDate"),
        "allotment_date": row.get("allotmentDate"),
        "listing_date": row.get("listingDate"),
        "price_max": price,
        "lot_size": row.get("lotSize"),
        "issue_size": row.get("issueSizeCr"),
        "subscription_total": row.get("subscriptionText"),
        "pe": row.get("priceToEarnings"),
        "gmp": {
            "price": gmp,
            "percentage": row.get("gmpPercent"),
            "updated_at": row.get("updatedOn"),
        },
    }, today)


def normalise(item, today):
    gmp = item.get("gmp") or {}
    return {
        "id": item["slug"],
        "name": item.get("display_name") or item.get("name") or item["slug"],
        "symbol": symbol_from(item),
        "type": ipo_type(item.get("type")),
        "status": status(item.get("status")),
        "datesTba": bool(item.get("dates_tba")),
        "logo": item.get("logo") or None,
        "webUrl": item.get("web_url") or None,
        "openDate": day(item.get("open_date")),
        "closeDate": day(item.get("close_date")),
        "allotmentDate": day(item.get("allotment_date")),
        "listingDate": day(item.get("listing_date")),
        "priceMin": num(item.get("price_min")),
        "priceMax": num(item.get("price_max")) or num(item.get("issue_price")),
        "lotSize": int(num(item.get("lot_size")) or 0) or None,
        "issueSizeCr": issue_size_cr(item.get("issue_size")),
        "subscriptionTotal": num(item.get("subscription_total")),
        "listingPrice": num(item.get("listing_price")),
        "currentPrice": num(item.get("cmp")),
        "pe": num(item.get("pe")),
        "gmp": None if num(gmp.get("price")) is None else {
            "price": num(gmp.get("price")),
            "percent": num(gmp.get("percentage")),
            "updatedAt": gmp.get("updated_at"),
        },
    }


def merge_history(prev_history, gmp, today):
    hist = {h["date"]: h["gmp"] for h in (prev_history or [])}
    if gmp is not None:
        hist[today] = gmp["price"]  # last reading of the day wins
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=MAX_HISTORY_DAYS)).isoformat()
    return [{"date": d, "gmp": g} for d, g in sorted(hist.items()) if d >= cutoff]


def keep(ipo, today):
    """Drop IPOs that listed long ago so the file stays small."""
    if ipo["status"] != "LISTED" or not ipo["listingDate"]:
        return True
    return ipo["listingDate"] >= (today - dt.timedelta(days=KEEP_LISTED_DAYS)).isoformat()


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "ipos.json"
    apify = os.environ.get("APIFY_TOKEN")
    ipoguru = os.environ.get("IPOGURU_API_KEY")
    if not apify and not ipoguru:
        sys.exit("Set APIFY_TOKEN or IPOGURU_API_KEY")

    prev = {}
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            prev = {i["id"]: i for i in json.load(f).get("ipos", [])}

    now = dt.datetime.now(IST)
    today = now.date()
    try:
        if apify:
            source = "InvestorGain via Apify"
            source_url = "https://www.investorgain.com"
            mapped = [normalise_apify(r, today) for r in fetch_apify(apify)]
        else:
            source = "IPO Guru (ipoguru.in)"
            source_url = "https://www.ipoguru.in"
            mapped = [normalise(i, today) for i in fetch_ipoguru(ipoguru) if i.get("slug")]
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        sys.exit(f"Fetch failed, keeping the previous file: {e}")
    if not mapped:
        sys.exit("Provider returned no IPOs, keeping the previous file")

    ipos = []
    for ipo in mapped:
        ipo["gmpHistory"] = merge_history(prev.get(ipo["id"], {}).get("gmpHistory"), ipo["gmp"], today.isoformat())
        if keep(ipo, today):
            ipos.append(ipo)

    doc = {
        "version": SCHEMA_VERSION,
        "source": source,
        "sourceUrl": source_url,
        "updatedAt": now.isoformat(timespec="seconds"),
        "ipos": sorted(ipos, key=lambda i: (i["openDate"] or "9999", i["id"])),
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"Wrote {len(ipos)} IPOs to {out}")


if __name__ == "__main__":
    main()
