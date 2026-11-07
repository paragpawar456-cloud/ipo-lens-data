# ipo-lens-data

Daily IPO data for the IPO Lens app. A scheduled GitHub Action runs `collect.py` once a day
(about 9 PM IST) and commits `ipos.json`, which the app downloads.

- Source: InvestorGain's IPO GMP report via the India IPO Tracker actor on Apify (`APIFY_TOKEN` secret).
- GMP is unofficial and unregulated. Not investment advice.
- To refresh now: Actions → Collect IPO data → Run workflow (or push a change to collect.py).
