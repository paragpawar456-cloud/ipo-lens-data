#!/usr/bin/env python3
"""
Fetches open, upcoming and recent IPOs with live GMP and subscription, and writes a normalised
ipos.json for the IPO Lens app. Keeps its own day-wise GMP history.

Two providers are supported; the first one with a credential set is used:
  APIFY_TOKEN      India IPO Tracker actor on Apify (reads InvestorGain's GMP report).
                   Free Apify plan includes $5/month; one daily run of ~40 rows costs about $0.12.
  IPOGURU_API_KEY  IPO Guru API (free plan: 10 requests/day, non-commercial).

Also keeps:
  listed.json  Listing results for the past 12 months (gmpPerformance report). The first run backfills
               last year and this year (~700 rows, about $2 once); later runs fetch only the newest rows.
  about.json   Company profiles from Wikipedia and Wikidata (free), looked up once per company.

Usage: APIFY_TOKEN=... python3 collect.py [path/to/ipos.json]
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request

IPOGURU_API = "https://www.ipoguru.in/api/v2/ipos?months=3"
APIFY_API = "https://api.apify.com/v2/acts/data_daemon~india-ipo-tracker/run-sync-get-dataset-items?format=json&clean=true"
SCHEMA_VERSION = 1
KEEP_LISTED_DAYS = 365
PERF_DAILY_ROWS = 15
ABOUT_LOOKUPS_PER_RUN = 80
ABOUT_RETRY_DAYS = 60
UA = "IPOLensDataBot/1.0 (https://github.com/paragpawar456-cloud/ipo-lens-data)"
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


def apify_rows(token, payload):
    req = urllib.request.Request(APIFY_API, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "IPOLens-collector/1.0",
    })
    with urllib.request.urlopen(req, timeout=300) as r:
        rows = json.load(r)
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected Apify response: {str(rows)[:300]}")
    return rows


def fetch_apify(token):
    rows = apify_rows(token, {"reports": ["gmp"], "category": "all", "maxItems": 50})
    return [row for row in rows if row.get("reportType", "gmp") == "gmp" and row.get("slug")]


def fetch_performance(token, year, max_items):
    """Listing results (issue price, listing price, gains, last traded price) for one calendar year."""
    rows = apify_rows(token, {"reports": ["gmpPerformance"], "category": "all", "year": year, "maxItems": max_items})
    return [r for r in rows if r.get("reportType") == "gmpPerformance" and r.get("slug")]


def update_listed(token, path, today):
    """Keeps listed.json, a rolling 12-month record of listing results keyed by slug."""
    cache = load_json(path, {})
    try:
        if cache:
            rows = fetch_performance(token, today.year, PERF_DAILY_ROWS)
        else:  # first run: backfill a full year
            rows = fetch_performance(token, today.year - 1, 0) + fetch_performance(token, today.year, 0)
        print(f"Listing results: {len(rows)} rows; first listing dates {[r.get('listingDate') for r in rows[:5]]}")
    except (OSError, RuntimeError, ValueError) as e:
        print(f"Listing results not refreshed: {e}")
        rows = []
    for r in rows:
        cache[r["slug"]] = {k: r.get(k) for k in (
            "ipoName", "category", "symbol", "listingDate", "issueSizeCr", "subscriptionX", "gmp", "gmpPercent",
            "issuePrice", "listingPrice", "listingGainPercent", "listingDayClosePrice", "lastTradedPrice")}
    cutoff = (today - dt.timedelta(days=KEEP_LISTED_DAYS)).isoformat()
    cache = {k: v for k, v in cache.items() if (day(v.get("listingDate")) or "9999") >= cutoff}
    save_json(path, cache)
    return cache


def from_performance(slug, r):
    """A past IPO that is only known from the listing results report."""
    price = num(r.get("issuePrice"))
    listing = day(r.get("listingDate"))
    gmp = num(r.get("gmp"))
    ipo = normalise({
        "slug": slug,
        "display_name": r.get("ipoName"),
        "type": r.get("category"),
        "status": "listed",
        "listing_date": listing,
        "price_max": price,
        "issue_size": r.get("issueSizeCr"),
        "subscription_total": r.get("subscriptionX"),
        "gmp": {"price": gmp, "percentage": r.get("gmpPercent"), "updated_at": None},
    }, None)
    if gmp is not None and listing:
        # The last grey market price before listing.
        ipo["gmpHistory"] = [{"date": (dt.date.fromisoformat(listing) - dt.timedelta(days=1)).isoformat(), "gmp": gmp}]
    apply_performance(ipo, r)
    return ipo


def apply_performance(ipo, r):
    """Adds listing results to an IPO we already know about."""
    ipo["listingPrice"] = num(r.get("listingPrice")) or ipo.get("listingPrice")
    ipo["currentPrice"] = num(r.get("lastTradedPrice")) or ipo.get("currentPrice")
    ipo["listingDate"] = ipo.get("listingDate") or day(r.get("listingDate"))
    ipo["priceMax"] = ipo.get("priceMax") or num(r.get("issuePrice"))
    ipo["subscriptionTotal"] = ipo.get("subscriptionTotal") or num(r.get("subscriptionX"))
    if ipo.get("listingPrice"):
        ipo["status"] = "LISTED"


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


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def get_json(url, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    time.sleep(0.3)  # be gentle with the free APIs
    return body


GENERIC = {"limited", "ltd", "ltd.", "india", "indian", "the", "and", "&", "co", "co.", "company", "pvt", "private", "ipo",
           "sme", "nse", "bse", "of", "corporation", "corp", "inc", "group"}
WIKI = "https://en.wikipedia.org/w/api.php"
WIKIDATA = "https://www.wikidata.org/w/api.php"


def words(name):
    return [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in GENERIC]


def wikipedia_profile(name):
    """Finds the company's Wikipedia article and Wikidata facts. None when there is no confident match."""
    key = words(re.sub(r"\bIPO\b", "", name))
    if not key:
        return None
    hits = get_json(WIKI, {"action": "query", "list": "search", "srsearch": " ".join(key) + " company India",
                           "srlimit": 5, "format": "json"})["query"]["search"]
    for hit in hits:
        title = hit["title"]
        if set(key) - set(words(title)) or "(disambiguation)" in title:
            continue
        pages = get_json(WIKI, {"action": "query", "prop": "extracts|pageprops", "exintro": 1, "explaintext": 1,
                                "exsentences": 5, "redirects": 1, "titles": title, "format": "json"})["query"]["pages"]
        page = next(iter(pages.values()))
        text = (page.get("extract") or "").strip()
        # Single-word names are ambiguous; require the article to be about an Indian company.
        if not text or not re.search(r"\bIndia", text) or not re.search(r"company|manufactur|firm|bank|provider|maker|producer|conglomerate|subsidiary", text, re.I):
            continue
        profile = {"summary": text, "source": "Wikipedia", "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(page["title"].replace(" ", "_"))}
        qid = page.get("pageprops", {}).get("wikibase_item")
        if qid:
            try:
                profile.update(wikidata_facts(qid))
            except (OSError, KeyError, ValueError) as e:
                print(f"Wikidata {qid}: {e}")
        return profile
    return None


def wikidata_facts(qid):
    claims = get_json(WIKIDATA, {"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"})["entities"][qid]["claims"]

    def values(prop):
        return [c["mainsnak"].get("datavalue", {}).get("value") for c in claims.get(prop, []) if c["mainsnak"].get("datavalue")]

    facts = {}
    inception = values("P571")
    if inception and isinstance(inception[0], dict):
        m = re.match(r"[+-]?(\d{4})", inception[0].get("time", ""))
        if m:
            facts["founded"] = int(m.group(1))
    employees = values("P1128")
    if employees and isinstance(employees[-1], dict):
        facts["employees"] = int(float(employees[-1]["amount"]))
    ref = {p: [v["id"] for v in values(p) if isinstance(v, dict) and "id" in v] for p in ("P159", "P452", "P1056")}
    ids = sorted({i for v in ref.values() for i in v[:6]})
    if ids:
        ents = get_json(WIKIDATA, {"action": "wbgetentities", "ids": "|".join(ids), "props": "labels", "languages": "en", "format": "json"})["entities"]
        label = lambda i: ents.get(i, {}).get("labels", {}).get("en", {}).get("value")
        if ref["P159"] and label(ref["P159"][0]):
            facts["headquarters"] = label(ref["P159"][0])
        facts["industries"] = [l for l in map(label, ref["P452"][:4]) if l]
        facts["products"] = [l for l in map(label, ref["P1056"][:6]) if l]
    return facts


def update_about(ipos, path, today):
    """Looks up each company once (a few per run) and stores the profile in about.json."""
    cache = load_json(path, {})
    retry = (today - dt.timedelta(days=ABOUT_RETRY_DAYS)).isoformat()
    todo = [i for i in ipos if i["id"] not in cache or (cache[i["id"]].get("none") and cache[i["id"]].get("checked", "") < retry)]
    # Current IPOs first, then the most recent listings.
    todo.sort(key=lambda i: (i["status"] == "LISTED", -int((i.get("listingDate") or "9999-12-31").replace("-", ""))))
    found = 0
    for ipo in todo[:ABOUT_LOOKUPS_PER_RUN]:
        try:
            profile = wikipedia_profile(ipo["name"])
        except (OSError, KeyError, ValueError) as e:
            print(f"Profile lookup failed for {ipo['name']}: {e}")
            continue
        cache[ipo["id"]] = profile or {"none": True, "checked": today.isoformat()}
        found += bool(profile)
    print(f"Company profiles: looked up {min(len(todo), ABOUT_LOOKUPS_PER_RUN)}, found {found}, {len(todo) - ABOUT_LOOKUPS_PER_RUN if len(todo) > ABOUT_LOOKUPS_PER_RUN else 0} left for later runs")
    save_json(path, cache)
    return cache


def details_url(ipo):
    """InvestorGain's IPO page has the full company profile; the GMP page link maps onto it."""
    url = ipo.get("webUrl") or ""
    if "/gmp/" in url:
        return url.replace("/gmp/", "/ipo/", 1)
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": f"{ipo['name']} IPO company profile"})


def merge_history(prev_history, gmp, today):
    hist = {h["date"]: h["gmp"] for h in (prev_history or [])}
    if gmp is not None:
        hist[today] = gmp["price"]  # last reading of the day wins
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=MAX_HISTORY_DAYS)).isoformat()
    return [{"date": d, "gmp": g} for d, g in sorted(hist.items()) if d >= cutoff]


def keep(ipo, today):
    """Keeps current IPOs and those that listed within the past 12 months."""
    cutoff = (today - dt.timedelta(days=KEEP_LISTED_DAYS)).isoformat()
    if ipo.get("listingDate"):
        return ipo["listingDate"] >= cutoff
    return (ipo.get("openDate") or "9999") >= cutoff


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

    base = os.path.dirname(os.path.abspath(out))
    listed = update_listed(apify, os.path.join(base, "listed.json"), today) if apify else {}

    by_id = {}
    for ipo in mapped:
        ipo["gmpHistory"] = merge_history(prev.get(ipo["id"], {}).get("gmpHistory"), ipo["gmp"], today.isoformat())
        by_id[ipo["id"]] = ipo
    # IPOs that dropped out of the live feed keep their last known details.
    for pid, old in prev.items():
        if pid not in by_id and (old.get("listingDate") or old.get("openDate")):
            by_id[pid] = old
    for slug, r in listed.items():
        if slug in by_id:
            apply_performance(by_id[slug], r)
        else:
            by_id[slug] = from_performance(slug, r)
    ipos = [i for i in by_id.values() if keep(i, today)]

    about = update_about(ipos, os.path.join(base, "about.json"), today)
    for ipo in ipos:
        profile = about.get(ipo["id"])
        ipo["about"] = None if not profile or profile.get("none") else profile
        ipo["detailsUrl"] = details_url(ipo)

    doc = {
        "version": SCHEMA_VERSION,
        "source": source,
        "sourceUrl": source_url,
        "updatedAt": now.isoformat(timespec="seconds"),
        "ipos": sorted(ipos, key=lambda i: (i.get("listingDate") or i.get("openDate") or "9999", i["id"]), reverse=True),
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"Wrote {len(ipos)} IPOs to {out}")


if __name__ == "__main__":
    main()
