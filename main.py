
import os
import re
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "LedgerAgent/1.0"})
TZ = ZoneInfo("Asia/Kolkata")

INDEX_URL = os.environ.get("LEDGER_INDEX_URL", "")


def get_json(url):
    r = SESSION.get(url, timeout=5)
    r.raise_for_status()
    return r.json()


def get_all_rows(url):
    """Read paginated JSON data, supporting common response formats."""
    rows = []
    seen = set()

    for page in range(1, 101):
        paged_url = url
        separator = "&" if "?" in url else "?"
        paged_url = re.sub(r"([&?])page=\d+", r"\1page=" + str(page),
                           paged_url)
        if not re.search(r"[?&]page=\d+", paged_url):
            paged_url += separator + "page=" + str(page)

        data = get_json(paged_url)

        if isinstance(data, list):
            batch = data
            more = len(batch) > 0
        elif isinstance(data, dict):
            batch = None
            for key in ("orders", "rows", "data", "results", "items"):
                if isinstance(data.get(key), list):
                    batch = data[key]
                    break
            if batch is None:
                batch = [data] if data else []
            more = bool(data.get("next")) or bool(data.get("has_more"))
            if not more and batch:
                more = len(batch) >= 100
        else:
            break

        new_rows = []
        for row in batch:
            marker = json.dumps(row, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                new_rows.append(row)

        rows.extend(new_rows)

        if not batch or not more:
            break

    return rows


def discover_data():
    if not INDEX_URL:
        raise RuntimeError("LEDGER_INDEX_URL is not configured")

    index = get_json(INDEX_URL)
    links = index.get("links", {})

    orders = get_all_rows(links["orders"])
    rates = get_json(links["rates"])
    exported = get_json(links["export"])

    # Keep the latest version of each order.
    current = {}
    for row in orders:
        oid = row.get("order_id", row.get("id"))
        if oid is None:
            continue
        old = current.get(str(oid))
        if old is None or str(row.get("updated_at", "")) > str(
            old.get("updated_at", "")
        ):
            current[str(oid)] = row

    return list(current.values()), rates, exported


def num(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def answer_question(question, orders, rates, exported):
    q = question.lower()
    paid = [o for o in orders if str(o.get("status", "")).lower() == "paid"]

    if any(w in q for w in ("revenue", "sales total", "total sales")):
        total = sum(num(o.get("total", o.get("amount",
                    o.get("total_amount", o.get("grand_total", 0)))))
                    for o in paid)
        return {"answer": round(total, 2)}

    if "refund" in q:
        total = sum(num(o.get("refund", o.get("refund_amount", 0)))
                    for o in orders)
        return {"answer": round(total, 2)}

    if any(w in q for w in ("how many orders", "order count",
                             "number of orders")):
        return {"answer": len(orders)}

    if "paid" in q and any(w in q for w in ("how many", "count", "number")):
        return {"answer": len(paid)}

    if "status" in q:
        counts = defaultdict(int)
        for o in orders:
            counts[str(o.get("status", "unknown"))] += 1
        return {"answer": dict(counts)}

    if "product" in q and any(w in q for w in ("best", "top", "most")):
        totals = defaultdict(float)
        for o in paid:
            product = str(o.get("product", o.get("product_name", "unknown")))
            totals[product] += num(o.get("quantity", 1))
        best = sorted(totals.items(), key=lambda x: x[1], reverse=True)
        return {"answer": best[:10]}

    if "customer" in q:
        totals = defaultdict(float)
        for o in paid:
            customer = str(o.get("customer", o.get("customer_name",
                            o.get("customer_id", "unknown"))))
            totals[customer] += num(o.get("total", o.get("amount",
                                      o.get("total_amount", 0))))
        best = sorted(totals.items(), key=lambda x: x[1], reverse=True)
        return {"answer": best[:10]}

    # Safe fallback: return metadata to help diagnose schema mismatches.
    return {
        "answer": "I could not determine the requested calculation.",
        "available_order_fields": sorted({
            k for o in orders for k in o.keys()
        }),
        "rates": rates,
        "export_summary": (
            list(exported.keys()) if isinstance(exported, dict)
            else type(exported).__name__
        )
    }


@app.post("/")
@app.post("/query")
async def query(request: Request):
    try:
        body = await request.json()
        question = body.get("question", "")
        if not isinstance(question, str) or not question.strip():
            return JSONResponse(
                {"error": "Provide a non-empty question"},
                status_code=400
            )

        orders, rates, exported = discover_data()
        result = answer_question(question, orders, rates, exported)
        return JSONResponse(result)

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=502
        )


@app.get("/")
async def health():
    return {"status": "ok"}
