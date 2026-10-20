"""Profit, insights, income statement and tax report calculations."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session, selectinload

from .. import settings
from ..models import BulkBuy, Expense, Item, Listing, MileageEntry, Sale
from ..util import utcnow

# (category, Schedule C line, description)
EXPENSE_CATEGORIES = [
    ("Advertising", "8", "Ads, promoted listings not tied to an order"),
    ("Car and truck expenses", "9", "Actual vehicle costs (if not using the mileage rate)"),
    ("Commissions and fees", "10", "Marketplace fees, store subscriptions, payment fees"),
    ("Contract labor", "11", "Help you pay that isn't payroll"),
    ("Depreciation", "13", "Equipment (consult your tax pro)"),
    ("Insurance", "15", "Business insurance"),
    ("Interest", "16b", "Business loan / card interest"),
    ("Legal and professional services", "17", "Accountant, tax prep, legal"),
    ("Office expense", "18", "Printer ink, paper, small office items"),
    ("Rent or lease – equipment", "20a", "Rented equipment"),
    ("Rent or lease – storage/property", "20b", "Storage unit, warehouse"),
    ("Repairs and maintenance", "21", "Repairs to business equipment"),
    ("Supplies", "22", "Boxes, mailers, tape, labels, cleaning supplies"),
    ("Taxes and licenses", "23", "Business licenses, permits"),
    ("Travel", "24a", "Overnight sourcing trips"),
    ("Meals (50% deductible)", "24b", "Business meals"),
    ("Utilities", "25", "Phone/internet business portion"),
    ("Shipping and postage", "27a", "Postage bought outside the marketplace"),
    ("Software and subscriptions", "27a", "Listing tools, apps, subscriptions"),
    ("Bank and payment fees", "27a", "Bank or processor fees"),
    ("Education", "27a", "Courses, books about your business"),
    ("Other expenses", "27a", "Anything else"),
    ("Inventory purchases (COGS)", "Part III", "Inventory not entered as individual items"),
]
CATEGORY_LINE = {c: line for c, line, _ in EXPENSE_CATEGORIES}

MARKETPLACES = ["eBay", "Amazon", "Poshmark", "Mercari", "Etsy", "Facebook Marketplace", "Depop",
                "Whatnot", "Grailed", "OfferUp", "Craigslist", "Shopify", "In person / Offline", "Other"]


# ---------------------------------------------------------------- periods
def earliest_date(db: Session) -> date:
    from sqlalchemy import func
    cands = [db.query(func.min(Sale.sale_date)).scalar(), db.query(func.min(Listing.start_time)).scalar(),
             db.query(func.min(Item.purchase_date)).scalar(), db.query(func.min(Expense.expense_date)).scalar()]
    ds = [c.date() if isinstance(c, datetime) else c for c in cands if c]
    return min(ds) if ds else utcnow().date()


def period_range(key: str, start: str | None = None, end: str | None = None,
                 db: Session | None = None) -> tuple[date, date, str]:
    today = utcnow().date()
    if key == "custom" and start and end:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        return s, e, f"{s:%b %d, %Y} – {e:%b %d, %Y}"
    if key == "7d":
        return today - timedelta(days=6), today, "Last 7 days"
    if key == "30d":
        return today - timedelta(days=29), today, "Last 30 days"
    if key == "ytd":
        return date(today.year, 1, 1), today, f"{today.year} year to date"
    if key == "lastyear":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), str(today.year - 1)
    if key == "12m":
        return today - timedelta(days=364), today, "Last 12 months"
    if key == "all":
        return (earliest_date(db) if db is not None else date(2000, 1, 1)), today, "All time"
    return today - timedelta(days=89), today, "Last 90 days"


def _dt(d: date, end: bool = False) -> datetime:
    return datetime.combine(d, datetime.max.time() if end else datetime.min.time())


def sales_in(db: Session, start: date, end: date) -> list[Sale]:
    return (db.query(Sale).options(selectinload(Sale.item))
            .filter(Sale.sale_date >= _dt(start), Sale.sale_date <= _dt(end, True))
            .order_by(Sale.sale_date.desc()).all())


def summarize(sales: list[Sale]) -> dict:
    live = [s for s in sales if s.status != "cancelled"]
    gross = sum(s.gross for s in live)
    fees = sum(s.marketplace_fees or 0 for s in live)
    ads = sum(s.ad_fees or 0 for s in live)
    ship = sum(s.shipping_cost or 0 for s in live)
    other = sum(s.other_fees or 0 for s in live)
    refunds = sum(s.refund_amount or 0 for s in live)
    cogs = sum(s.cogs or 0 for s in live)
    net = gross - fees - ads - ship - other - refunds - cogs
    with_cost = [s for s in live if s.cogs]
    roi_base = sum(s.cogs for s in with_cost)
    roi_net = sum(s.net_profit for s in with_cost)
    units = sum(s.quantity or 1 for s in live)
    return {
        "orders": len({(s.marketplace, s.order_id) for s in live}),
        "units": units,
        "gross": round(gross, 2), "fees": round(fees, 2), "ads": round(ads, 2),
        "shipping": round(ship, 2), "other": round(other, 2), "refunds": round(refunds, 2),
        "cogs": round(cogs, 2), "net": round(net, 2),
        "margin": round(net / gross * 100, 1) if gross else None,
        "roi": round(roi_net / roi_base * 100, 1) if roi_base else None,
        "avg_sale": round(gross / units, 2) if units else None,
        "missing_cost": sum(1 for s in live if s.cogs is None),
        "cancelled": len(sales) - len(live),
    }


def insights(db: Session, start: date, end: date) -> dict:
    sales = sales_in(db, start, end)
    summ = summarize(sales)
    live = [s for s in sales if s.status != "cancelled"]

    # time series (daily if <= 120 days, else weekly / monthly)
    span = (end - start).days + 1
    bucket = "day" if span <= 120 else ("week" if span <= 730 else "month")

    def key(d: date) -> str:
        if bucket == "day":
            return d.isoformat()
        if bucket == "week":
            return (d - timedelta(days=d.weekday())).isoformat()
        return d.strftime("%Y-%m")

    keys: list[str] = []
    d = start
    while d <= end:
        k = key(d)
        if not keys or keys[-1] != k:
            keys.append(k)
        d += timedelta(days=1)
    series = {k: {"gross": 0.0, "net": 0.0, "sales": 0, "listings": 0} for k in keys}
    for s in live:
        k = key(s.sale_date.date())
        if k in series:
            series[k]["gross"] += s.gross
            series[k]["net"] += s.net_profit
            series[k]["sales"] += s.quantity or 1
    for lst in db.query(Listing).filter(Listing.start_time >= _dt(start), Listing.start_time <= _dt(end, True),
                                        Listing.previous_ebay_item_id.is_(None)).all():
        k = key(lst.start_time.date())
        if k in series:
            series[k]["listings"] += 1
    # by marketplace
    by_mkt: dict[str, dict] = defaultdict(lambda: {"gross": 0.0, "net": 0.0, "count": 0})
    for s in live:
        m = by_mkt[s.marketplace]
        m["gross"] += s.gross
        m["net"] += s.net_profit
        m["count"] += s.quantity or 1

    # by source
    by_src: dict[str, dict] = defaultdict(lambda: {"net": 0.0, "count": 0, "cost": 0.0, "gross": 0.0})
    for s in live:
        src = (s.item.source if s.item and s.item.source else "Unknown")
        r = by_src[src]
        r["net"] += s.net_profit
        r["gross"] += s.gross
        r["cost"] += s.cogs or 0
        r["count"] += s.quantity or 1
    top_sources = sorted(by_src.items(), key=lambda kv: kv[1]["net"], reverse=True)[:10]
    listings_by_source: dict[str, int] = defaultdict(int)
    for it in db.query(Item).filter(Item.status == "listed").all():
        listings_by_source[it.source or "Unknown"] += 1

    # buyers
    states: dict[str, int] = defaultdict(int)
    countries: dict[str, int] = defaultdict(int)
    for s in live:
        if s.buyer_country:
            countries[s.buyer_country] += 1
        if s.buyer_state and (s.buyer_country in (None, "US", "CA", "AU")):
            states[s.buyer_state] += 1

    # timings
    to_list = []
    for it in db.query(Item).filter(Item.listed_at >= _dt(start), Item.listed_at <= _dt(end, True),
                                    Item.purchase_date.isnot(None)).all():
        days = (it.listed_at.date() - it.purchase_date).days
        if days >= 0:
            to_list.append(days)
    to_sell = []
    for s in live:
        if s.item and s.item.listed_at:
            days = (s.sale_date - s.item.listed_at).days
            if days >= 0:
                to_sell.append(days)

    # spend
    spend = 0.0
    for it in db.query(Item).filter(Item.purchase_date >= start, Item.purchase_date <= end,
                                    Item.bulk_buy_id.is_(None)).all():
        spend += it.cost or 0
    for bb in db.query(BulkBuy).filter(BulkBuy.purchase_date >= start, BulkBuy.purchase_date <= end).all():
        spend += bb.total_cost or 0

    return {
        "summary": summ, "sales": sales, "bucket": bucket,
        "series": {"labels": keys, **{f: [round(series[k][f], 2) for k in keys] for f in ("gross", "net", "sales", "listings")}},
        "by_marketplace": sorted(by_mkt.items(), key=lambda kv: kv[1]["net"], reverse=True),
        "top_sources": top_sources,
        "listings_by_source": sorted(listings_by_source.items(), key=lambda kv: kv[1], reverse=True)[:10],
        "states": sorted(states.items(), key=lambda kv: kv[1], reverse=True),
        "countries": sorted(countries.items(), key=lambda kv: kv[1], reverse=True),
        "avg_days_to_list": round(sum(to_list) / len(to_list), 1) if to_list else None,
        "avg_days_to_sell": round(sum(to_sell) / len(to_sell), 1) if to_sell else None,
        "inventory_spend": round(spend, 2),
        "new_listings": sum(series[k]["listings"] for k in keys),
    }


def inventory_snapshot(db: Session) -> dict:
    items = db.query(Item).filter(Item.status.in_(("unlisted", "listed"))).all()
    listed = [i for i in items if i.status == "listed"]
    unlisted = [i for i in items if i.status == "unlisted"]

    def remaining_cost(i: Item) -> float:
        if i.unit_cost is None:
            return 0.0
        return i.unit_cost * max((i.quantity or 1) - (i.quantity_sold or 0), 0)

    active_value = sum((lst.price or 0) * max((lst.quantity or 1) - (lst.quantity_sold or 0), 0)
                       for lst in db.query(Listing).filter(Listing.status == "active").all())
    return {
        "listed": len(listed), "unlisted": len(unlisted),
        "listed_cost": round(sum(remaining_cost(i) for i in listed), 2),
        "unlisted_cost": round(sum(remaining_cost(i) for i in unlisted), 2),
        "active_value": round(active_value, 2),
        "missing_cost": sum(1 for i in items if i.cost is None),
    }


# ---------------------------------------------------------------- P&L
def mileage_deduction(db: Session, start: date, end: date) -> tuple[float, float]:
    miles = 0.0
    amount = 0.0
    for m in db.query(MileageEntry).filter(MileageEntry.trip_date >= start, MileageEntry.trip_date <= end).all():
        miles += m.total_miles
        amount += m.total_miles * settings.mileage_rate(m.trip_date.year)
    return round(miles, 1), round(amount, 2)


def expenses_by_category(db: Session, start: date, end: date) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for e in db.query(Expense).filter(Expense.expense_date >= start, Expense.expense_date <= end).all():
        out[e.category] += e.amount or 0
    return {k: round(v, 2) for k, v in sorted(out.items())}


def income_statement(db: Session, start: date, end: date) -> dict:
    sales = [s for s in sales_in(db, start, end) if s.status != "cancelled"]
    mk: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for s in sales:
        r = mk[s.marketplace]
        r["sales"] += s.sale_price or 0
        r["shipping_income"] += s.shipping_charged or 0
        r["refunds"] += s.refund_amount or 0
        r["cogs"] += s.cogs or 0
        r["fees"] += s.marketplace_fees or 0
        r["ads"] += s.ad_fees or 0
        r["shipping"] += s.shipping_cost or 0
        r["other"] += s.other_fees or 0
    tot: dict[str, float] = defaultdict(float)
    for r in mk.values():
        for k, v in r.items():
            tot[k] += v
    revenue = tot["sales"] + tot["shipping_income"] - tot["refunds"]
    gross_profit = revenue - tot["cogs"]
    selling = tot["fees"] + tot["ads"] + tot["shipping"] + tot["other"]
    exp = expenses_by_category(db, start, end)
    inv_purchases = exp.pop("Inventory purchases (COGS)", 0.0)
    miles, mileage = mileage_deduction(db, start, end)
    opex = sum(exp.values()) + mileage
    net = gross_profit - selling - opex
    return {
        "by_marketplace": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in sorted(mk.items())},
        "total": {k: round(v, 2) for k, v in tot.items()},
        "revenue": round(revenue, 2), "gross_profit": round(gross_profit, 2),
        "selling": round(selling, 2), "expenses": exp, "mileage": mileage, "miles": miles,
        "opex": round(opex, 2), "net": round(net, 2),
        "inventory_purchases_unitemized": round(inv_purchases, 2),
        "missing_cost": sum(1 for s in sales if s.cogs is None),
    }


# ---------------------------------------------------------------- tax report
def _sold_qty_before(item: Item, when: datetime) -> int:
    return sum(s.quantity or 1 for s in item.sales
               if s.status != "cancelled" and not s.restocked and s.sale_date < when)


def _acquired(item: Item) -> date:
    if item.purchase_date:
        return item.purchase_date
    if item.bulk_buy and item.bulk_buy.purchase_date:
        return item.bulk_buy.purchase_date
    if item.listed_at:
        return item.listed_at.date()
    return item.created_at.date()


def inventory_value_at(db: Session, when: date, items: list[Item] | None = None) -> float:
    """Cost of goods on hand at the start of `when`."""
    cutoff = _dt(when)
    total = 0.0
    for it in items if items is not None else db.query(Item).options(selectinload(Item.sales)).all():
        if it.unit_cost is None or _acquired(it) >= when:
            continue
        if it.status == "written_off" and it.written_off_at and it.written_off_at < when:
            continue
        remaining = (it.quantity or 1) - _sold_qty_before(it, cutoff)
        if remaining > 0:
            total += it.unit_cost * remaining
    return round(total, 2)


def tax_report(db: Session, year: int, method: str | None = None) -> dict:
    method = method or settings.get("TAX_METHOD") or "accrual"
    start, end = date(year, 1, 1), date(year, 12, 31)
    items = db.query(Item).options(selectinload(Item.sales), selectinload(Item.bulk_buy)).all()
    all_sales = [s for s in sales_in(db, start, end) if s.status != "cancelled"]

    gross_receipts = sum(s.gross for s in all_sales)
    if method == "cash":
        refunds = sum(s.refund_amount or 0 for s in db.query(Sale).filter(
            Sale.refund_date >= _dt(start), Sale.refund_date <= _dt(end, True), Sale.status != "cancelled").all())
        refunds += sum(s.refund_amount or 0 for s in all_sales if s.refund_amount and not s.refund_date)
    else:
        refunds = sum(s.refund_amount or 0 for s in all_sales)

    exp = expenses_by_category(db, start, end)
    unitemized_inventory = exp.pop("Inventory purchases (COGS)", 0.0)

    # Part III
    purchases = 0.0
    for it in items:
        if it.cost is None or it.bulk_buy_id:
            continue
        if start <= _acquired(it) <= end:
            purchases += it.cost
    bulk_spend = 0.0
    for bb in db.query(BulkBuy).filter(BulkBuy.purchase_date >= start, BulkBuy.purchase_date <= end).all():
        bulk_spend += bb.total_cost or 0
    # bulk-buy items with no bulk date use their own acquired date
    for it in items:
        if it.bulk_buy_id and it.cost and it.bulk_buy and not it.bulk_buy.purchase_date and start <= _acquired(it) <= end:
            bulk_spend += it.cost
    purchases += bulk_spend + unitemized_inventory

    if method == "cash":
        begin_inv = end_inv = 0.0
        cogs = purchases
    else:
        begin_inv = inventory_value_at(db, start, items)
        end_inv = inventory_value_at(db, end + timedelta(days=1), items)
        cogs = begin_inv + purchases - end_inv

    fees = sum(s.marketplace_fees or 0 for s in all_sales) + sum(s.other_fees or 0 for s in all_sales)
    ads = sum(s.ad_fees or 0 for s in all_sales)
    labels = sum(s.shipping_cost or 0 for s in all_sales)
    miles, mileage = mileage_deduction(db, start, end)

    lines: dict[str, dict] = {}

    def add(line: str, label: str, amount: float) -> None:
        if not amount:
            return
        row = lines.setdefault(line + "|" + label, {"line": line, "label": label, "amount": 0.0})
        row["amount"] += amount

    add("8", "Advertising (promoted listings)", ads)
    add("9", f"Car and truck — standard mileage ({miles:,.1f} mi)", mileage)
    add("10", "Commissions and fees (marketplace fees)", fees)
    add("27a", "Shipping labels / postage (marketplace)", labels)
    for cat, amt in exp.items():
        line = CATEGORY_LINE.get(cat, "27a")
        if cat == "Meals (50% deductible)":
            add(line, cat + " — 50% of " + f"{amt:,.2f}", amt * 0.5)
        else:
            add(line, cat, amt)

    def line_sort(r):
        num = "".join(ch for ch in r["line"] if ch.isdigit())
        return (int(num) if num else 99, r["line"])

    expense_lines = sorted(({**r, "amount": round(r["amount"], 2)} for r in lines.values()), key=line_sort)
    total_expenses = round(sum(r["amount"] for r in expense_lines), 2)
    gross_income = gross_receipts - refunds - cogs
    net_profit = gross_income - total_expenses

    checks = data_checks(db, year)
    by_mkt: dict[str, float] = defaultdict(float)
    for s in all_sales:
        by_mkt[s.marketplace] += s.gross
    return {
        "year": year, "method": method,
        "line1": round(gross_receipts, 2), "line2": round(refunds, 2),
        "line3": round(gross_receipts - refunds, 2), "line4": round(cogs, 2),
        "line5": round(gross_receipts - refunds - cogs, 2),
        "part3": {"begin": round(begin_inv, 2), "purchases": round(purchases, 2),
                  "end": round(end_inv, 2), "cogs": round(cogs, 2)},
        "expense_lines": expense_lines, "line28": total_expenses, "line31": round(net_profit, 2),
        "gross_by_marketplace": {k: round(v, 2) for k, v in sorted(by_mkt.items())},
        "miles": miles, "checks": checks,
        "tax_collected": round(sum(s.tax_collected or 0 for s in all_sales), 2),
    }


def data_checks(db: Session, year: int | None = None) -> list[dict]:
    q = db.query(Sale).options(selectinload(Sale.item)).filter(Sale.status != "cancelled")
    if year:
        q = q.filter(Sale.sale_date >= datetime(year, 1, 1), Sale.sale_date < datetime(year + 1, 1, 1))
    sales = q.all()
    no_cost = {s.item_id for s in sales if s.item_id and s.cogs is None}
    unlinked = [s for s in sales if s.item_id is None]
    sold_items = db.query(Item).filter(Item.id.in_(no_cost)).all() if no_cost else []
    item_ids = {s.item_id for s in sales if s.item_id}
    no_date = db.query(Item).filter(Item.id.in_(item_ids), Item.purchase_date.is_(None),
                                    Item.bulk_buy_id.is_(None)).count() if item_ids else 0
    no_source = db.query(Item).filter(Item.id.in_(item_ids), Item.source.is_(None),
                                      Item.bulk_buy_id.is_(None)).count() if item_ids else 0
    unl_cost = db.query(Item).filter(Item.status.in_(("unlisted", "listed")), Item.cost.is_(None)).count()
    return [
        {"key": "sold_no_cost", "label": "Sold items missing a cost", "count": len(sold_items),
         "severity": "high", "help": "Without a cost, cost of goods sold is understated and you'll overpay tax.",
         "link": "/inventory?status=sold&missing=cost" + (f"&year={year}" if year else "")},
        {"key": "sold_no_date", "label": "Sold items missing a purchase date", "count": no_date,
         "severity": "medium", "help": "Needed to place inventory in the right tax year.",
         "link": "/inventory?status=sold&missing=date"},
        {"key": "sold_no_source", "label": "Sold items missing a source", "count": no_source,
         "severity": "low", "help": "Optional, but powers 'top sources' insights.",
         "link": "/inventory?status=sold&missing=source"},
        {"key": "unlinked_sales", "label": "Sales not linked to an inventory item", "count": len(unlinked),
         "severity": "medium", "help": "Link them so the item's cost is counted.", "link": "/sales?unlinked=1"},
        {"key": "inventory_no_cost", "label": "Current inventory missing a cost", "count": unl_cost,
         "severity": "low", "help": "Affects ending inventory value.", "link": "/inventory?missing=cost"},
    ]
