"""Build the compact app-data.json (schema v1) consumed by the Earnings DashBoard app."""


def build_app_data(recs, generated):
    stocks = []
    for r in recs:
        if r.get("mkt") != "IN":
            continue
        stocks.append({
            "s": r["sym"][:-3] if r["sym"].endswith(".NS") else r["sym"],
            "n": r.get("name"),
            "ind": r.get("ind"),
            "next": r.get("next_date"),
            "last": r.get("last_date"),
            "pl": r.get("prof_label"),
            "q": r.get("q") or [],
            "src": r.get("src"),
        })
    stocks.sort(key=lambda s: (s["n"] or s["s"]).lower())
    return {"v": 1, "generated": generated, "stocks": stocks}
