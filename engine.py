"""FairFreeze deterministic core. Every amount, date and check the officer sees comes from here, never from Gemini.

Tracing uses the lowest intermediate balance rule (LIBR): disputed money can only still be in the account
if the balance never fell below it after it arrived. Later deposits do not refill it.
"""
import difflib
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
RULES = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))
CASES = {k: v for k, v in json.loads((HERE / "cases.json").read_text(encoding="utf-8")).items() if not k.startswith("_")}
LIBRARY = json.loads((HERE / "library.json").read_text(encoding="utf-8"))


def ts(s):
    return datetime.fromisoformat(s)


def with_balances(case):
    bal, rows = case["opening_balance"], []
    for t in case["txns"]:
        bal += t["amount"]
        rows.append({**t, "balance": bal})
    return rows


def find(rows, txn_id):
    return next(i for i, r in enumerate(rows) if r["id"] == txn_id)


def trace(case, disputed=None):
    """Hold plan for an inbound disputed credit."""
    rows = with_balances(case)
    i = find(rows, case["flagged"])
    flagged = rows[i]
    disputed = disputed or flagged["amount"]
    lowest_after = min(r["balance"] for r in rows[i:])
    traceable = max(0, min(disputed, lowest_after))
    balance = rows[-1]["balance"]
    hold = min(disputed, balance)
    # money that left soon after: the lead for the next hop
    out_soon = [r for r in rows[i + 1:] if r["amount"] < 0 and ts(r["ts"]) - ts(flagged["ts"]) <= timedelta(hours=24)]
    return {
        "rows": rows, "flagged": flagged, "disputed": disputed, "balance": balance,
        "traceable": traceable, "dissipated": disputed - traceable,
        "hold": hold, "release": balance - hold,
        "index_whole_account": round(balance / disputed, 1),
        "index_fairfreeze": round(hold / disputed, 2),
        "next_hops": [{"party": r["party"], "amount": -r["amount"], "ts": r["ts"],
                       "minutes_after": int((ts(r["ts"]) - ts(flagged["ts"])).total_seconds() // 60)} for r in out_soon],
    }


def signals(case):
    """Behaviour facts from the bank's own record. Each says which way it points; none is a verdict."""
    rows = with_balances(case)
    f = rows[find(rows, case["flagged"])]
    t0 = ts(f["ts"])
    age = (t0 - datetime.fromisoformat(case["customer"]["opened"])).days
    regular = set(case.get("regular_payees", []))
    day = [r for r in rows if abs(ts(r["ts"]) - t0) <= timedelta(hours=24)]
    payers = {r["party"] for r in day if r["amount"] > 0}
    new_out = [r for r in day if r["amount"] < 0 and r["party"] not in regular and ts(r["ts"]) >= t0]
    passed_on = sum(-r["amount"] for r in new_out)
    ins = sum(r["amount"] for r in day if r["amount"] > 0)
    ratio = passed_on / ins if ins else 0
    regular_out = [r for r in day if r["amount"] < 0 and r["party"] in regular and ts(r["ts"]) >= t0]
    out = [
        {"name": "Account age", "value": f"{age // 365} years" if age >= 365 else f"{age} days",
         "points": "innocent" if age >= 365 else "concern"},
        # many payers is normal for a shop; it only counts against a new account
        {"name": "Different payers within 24 hours", "value": str(len(payers)),
         "points": "concern" if len(payers) >= 3 and age < 365 else "neutral"},
        {"name": "Share of 24h inflow sent on to new payees", "value": f"{ratio:.0%}",
         "points": "concern" if ratio >= 0.7 else "innocent" if ratio <= 0.2 else "neutral"},
    ]
    if regular_out:
        out.append({"name": "Outflow went to a long-standing payee", "value": regular_out[0]["party"], "points": "innocent"})
    regular_in = [r for r in rows if r["amount"] > 0 and r["party"] in regular]
    if regular_in:
        out.append({"name": "Regular income on the account", "value": regular_in[0]["party"], "points": "innocent"})
    return out


def _norm_name(s):
    return " ".join(sorted(re.sub(r"[^a-z ]", " ", (s or "").lower()).split()))


def name_match(a, b):
    """Initials-aware: 'AMIT K SHARMA' vs 'Amit Sharma' -> True."""
    ta, tb = _norm_name(a).split(), _norm_name(b).split()
    if not ta or not tb:
        return False
    long_a, long_b = {t for t in ta if len(t) > 1}, {t for t in tb if len(t) > 1}
    return len(long_a & long_b) >= min(2, len(long_a), len(long_b)) or \
        difflib.SequenceMatcher(None, _norm_name(a), _norm_name(b)).ratio() >= 0.85


def cross_check(case, items):
    """Compare what the customer's documents say with the bank's own record of the flagged credit."""
    f = next(t for t in case["txns"] if t["id"] == case["flagged"])
    paid = ts(f["ts"]).date()
    checks = []
    amounts = [i["amount"] for i in items if i.get("amount")]
    checks.append({"check": "Amount in the documents matches the credit",
                   "ok": any(abs(a - f["amount"]) < 1 for a in amounts),
                   "detail": f"bank record {f['amount']:,}; documents {', '.join(f'{a:,.0f}' for a in amounts) or 'none'}"})
    names = [i.get("counterparty_name") for i in items if i.get("counterparty_name")]
    checks.append({"check": "Buyer named in the documents is the payer",
                   "ok": any(name_match(n, f["party"]) for n in names),
                   "detail": f"bank record {f['party']}; documents {', '.join(names) or 'none'}"})
    dates = [i["date"] for i in items if i.get("date")]
    near = [d for d in dates if abs((datetime.fromisoformat(d).date() - paid).days) <= 3]
    checks.append({"check": "Document dates fit the payment date",
                   "ok": bool(near), "detail": f"payment {paid}; documents {', '.join(dates) or 'none'}"})
    listed = [i["listing_date"] for i in items if i.get("listing_date")]
    if listed:
        checks.append({"check": "Item was listed for sale before the payment",
                       "ok": any(datetime.fromisoformat(d).date() <= paid for d in listed),
                       "detail": f"listed {', '.join(listed)}"})
    shipped = [i["date"] for i in items if i.get("kind") == "courier_receipt" and i.get("date")]
    if shipped:
        checks.append({"check": "Goods were sent after the payment",
                       "ok": any(datetime.fromisoformat(d).date() >= paid for d in shipped),
                       "detail": f"courier date {', '.join(shipped)}"})
    tamper = [s for i in items for s in i.get("tamper_signs", [])]
    checks.append({"check": "No signs of editing in the images", "ok": not tamper, "detail": "; ".join(tamper) or "none seen"})
    return checks


def check_request(case, req):
    """Is the freeze request about this account and this money, and is it within the country's rules?"""
    r = RULES[case["country"]]
    acct = lambda s: re.sub(r"\W", "", s or "").upper()  # noqa: E731
    out = [{"check": "Account number matches", "ok": acct(req["account_number"]) == acct(case["customer"]["account"]),
            "detail": f"request {req['account_number']}; bank {case['customer']['account']}"},
           {"check": "Account holder matches", "ok": name_match(req["account_holder_latin"], case["customer"]["name"]),
            "detail": f"request {req['account_holder_latin']}; bank {case['customer']['name']}"},
           {"check": "Legal basis is one this country allows", "detail": req["legal_basis"] or "none cited",
            "ok": any(k in (req["legal_basis"] or "").lower() for k in r["allowed_legal_bases"])}]
    if case["kind"] == "inbound":
        f = next(t for t in case["txns"] if t["id"] == case["flagged"])
        ref = re.sub(r"\D", "", req["transaction_reference"] or "")
        out.append({"check": "Disputed transaction found on the account",
                    "ok": (bool(ref) and ref in f["desc"]) or abs(req["amount"] - f["amount"]) < 1,
                    "detail": f"{f['desc']} from {f['party']}, {f['amount']:,} on {f['ts'][:10]}"})
        out.append({"check": "Amount matches the bank record", "ok": abs(req["amount"] - f["amount"]) < 1,
                    "detail": f"request {req['amount']:,.0f}; bank {f['amount']:,}"})
        whole = req["action_requested"] == "freeze_whole_account"
        out.append({"check": "Scope: hold limited to the disputed amount", "ok": not whole, "over_freeze": whole,
                    "detail": (f"Request asks to freeze the whole account; {r['rules'][1 if case['country'] == 'IN' else 0]['id']} "
                               "limits the hold to the disputed amount") if whole else "Request is limited to the amount"})
    else:
        out.append({"check": "Order length within the legal limit (30 days)", "ok": 0 < req["validity_days"] <= 30,
                    "detail": f"{req['validity_days']} days"})
    return out


def reuse(image_hashes, script_text, own_case):
    """Has this evidence, or this story, been seen in other cases? Exact image hash + text similarity."""
    norm = lambda s: re.sub(r"\W+", " ", (s or "").lower()).strip()  # noqa: E731
    text = norm(script_text)
    hits = []
    for p in LIBRARY:
        if p["case"] == own_case:
            continue
        same_image = bool(set(image_hashes) & set(p.get("image_sha256", [])))
        sim = difflib.SequenceMatcher(None, text, norm(p["script"])).ratio() if text else 0
        if same_image or sim >= 0.75:
            hits.append({"case": p["case"], "bank": p["bank"], "date": p["date"],
                         "same_image": same_image, "similarity": round(sim, 2)})
    return hits


def strength(checks, sigs, hits):
    """Evidence strength for the officer. Deterministic, explainable, never a guilt score."""
    if len(hits) >= 3:
        return "red_flag"
    passed = sum(c["ok"] for c in checks)
    concern = sum(s["points"] == "concern" for s in sigs)
    if concern >= 2:
        return "red_flag" if passed < len(checks) else "weak"
    if passed == len(checks):
        return "strong"
    return "moderate" if passed >= len(checks) - 1 else "weak"


def plan(case, strength_level):
    """What the officer is asked to approve, per the country rule pack."""
    t = trace(case)
    if strength_level == "red_flag":
        return {"action": "keep_full_hold_escalate", "hold": t["balance"], "release": 0, "notify_customer": False,
                "why": "Strong mule indicators: the bank may hold beyond the disputed amount (IN-2) and must not tip off the holder."}
    if strength_level in ("strong", "moderate"):
        return {"action": "release_rest_keep_disputed", "hold": t["hold"], "release": t["release"], "notify_customer": True,
                "why": "Hold only the disputed amount; release the rest now and send the release memo with the evidence pack."}
    return {"action": "hold_disputed_request_more", "hold": t["hold"], "release": t["release"], "notify_customer": True,
            "why": "Hold only the disputed amount; ask the customer for one more document before a release memo."}


def essentials(case):
    """Singapore restriction order: keep daily living money open. Average essential spend x 1.2, rounded up to 50."""
    cats = set(case["essential_categories"])
    months = defaultdict(float)
    for t in case["txns"]:
        if t.get("cat") in cats:
            months[t["ts"][:7]] -= t["amount"]
    avg = sum(months.values()) / len(months)
    rows = with_balances(case)
    keep = sorted({t["party"] for t in case["txns"] if t.get("cat") in cats})
    new_payees = sorted({t["party"] for t in case["txns"] if t.get("cat") == "transfer"} | {p["party"] for p in case.get("pending", [])})
    sent = -sum(t["amount"] for t in case["txns"] if t.get("cat") == "transfer")
    return {"monthly": {k: round(v, 2) for k, v in sorted(months.items())}, "average": round(avg, 2),
            "allowance": math.ceil(avg * 1.2 / 50) * 50, "balance": rows[-1]["balance"],
            "keep_paying": keep, "blocked_payees": new_payees, "already_sent": sent,
            "blocked_pending": -sum(p["amount"] for p in case.get("pending", [])), "rows": rows}


def deadline(case, received_at):
    r = RULES[case["country"]]
    due = received_at + timedelta(hours=r["review_deadline_hours"])
    return {"received": received_at.isoformat(timespec="minutes"), "due": due.isoformat(timespec="minutes"),
            "label": r["deadline_label"]}


def restitution(pool, claims):
    """Split what is left in a mule pool between victims pro rata. Amounts in whole currency units, total preserved."""
    total = sum(claims.values())
    if total <= pool:
        return dict(claims)
    shares = {k: math.floor(pool * v / total) for k, v in claims.items()}
    left = pool - sum(shares.values())
    for k in sorted(claims, key=lambda k: -claims[k])[:left]:
        shares[k] += 1
    return shares


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def self_check():
    t = trace(CASES["IN-2026-0411"])
    assert (t["balance"], t["hold"], t["release"], t["index_whole_account"]) == (84000, 3600, 80400, 23.3), t
    x = trace(CASES["IN-2026-0419"])
    assert (x["traceable"], x["balance"]) == (1100, 1900), x
    assert x["next_hops"][0]["party"] == "GLOBALPAY WALLET 9" and x["next_hops"][0]["minutes_after"] == 16
    n = trace(CASES["TH-2026-1520"])
    assert (n["hold"], n["release"]) == (1250, 5480), n
    e = essentials(CASES["SG-2026-0077"])
    assert e["allowance"] == 1100 and e["already_sent"] == 35000, e
    assert name_match("AMIT K SHARMA", "Amit Sharma") and not name_match("AMIT K SHARMA", "Rahul Verma")
    assert restitution(1100, {"a": 24500, "b": 12000}) == {"a": 739, "b": 361}
    assert strength([{"ok": True}] * 4, [{"points": "innocent"}], []) == "strong"
    assert strength([{"ok": True}] * 4, [], [{}] * 3) == "red_flag"
    return True


if __name__ == "__main__":
    print("self-check", self_check())
