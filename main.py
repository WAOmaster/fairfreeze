"""FairFreeze API. Gemini reads documents and writes letters; engine.py does every number and check; an officer approves.

Credentials: on Cloud Run set GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION
(service account, no key). Locally set GEMINI_API_KEY. The key never reaches the browser.
"""
import asyncio
import functools
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

import engine

# Two tiers. READ: cheap, fast extraction into fixed schemas. REASON: the Case Agent that plans its own
# investigation with tools and writes the legal papers. Each list falls back left to right.
READ = os.environ.get("GEMINI_READ_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash").split(",")
REASON = os.environ.get("GEMINI_REASON_MODELS", "gemini-4-argon,gemini-3.8-flash").split(",")
MAX_BYTES = 10 * 1024 * 1024
MAGIC = {b"%PDF": "application/pdf", b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg"}
HERE = Path(__file__).parent
SAMPLES = {p.name for p in (HERE / "samples").glob("*.png")}
SGT = timezone(timedelta(hours=8))

app = FastAPI(title="FairFreeze")
creds = None
if os.environ.get("GCLOUD_ACCESS_TOKEN"):  # local dev against Vertex without ADC
    from google.oauth2.credentials import Credentials
    creds = Credentials(os.environ["GCLOUD_ACCESS_TOKEN"])
client = genai.Client(credentials=creds) if creds else genai.Client()
assert engine.self_check()

# Case state lives in Firestore (collection "cases") so it survives restarts; memory is a write-through cache.
# One Cloud Run instance (max-instances=1) keeps the cache consistent.
db = None
if os.environ.get("FIRESTORE", "on") == "on":
    from google.cloud import firestore
    db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"), credentials=creds)
START = datetime.now(SGT).replace(second=0, microsecond=0)
SAVED = ("status", "audit", "request", "evidence", "drafts", "decision")


def fresh(cid):
    # received is relative to start so the demo deadlines always look live
    return {"received": START - timedelta(hours=engine.CASES[cid]["received_hours_ago"]), "status": "New", "audit": [],
            "request": None, "evidence": None, "drafts": None, "decision": None, "running": False, "live_steps": [],
            "agent_error": None}


def save(cid):
    if db:
        db.collection("cases").document(cid).set({k: STATE[cid][k] for k in SAVED})


STATE = {cid: fresh(cid) for cid in engine.CASES}
if db:
    for cid in STATE:
        snap = db.collection("cases").document(cid).get()
        if snap.exists:
            STATE[cid].update({k: v for k, v in snap.to_dict().items() if k in SAVED})


# ---------- schemas Gemini must fill ----------
class Requisition(BaseModel):
    authority: str = Field(description="Issuing body, in English")
    officer: str
    reference_no: str
    complaint_or_case_no: str = Field(description="Complaint, case or order number; empty if none")
    date_issued: str = Field(description="YYYY-MM-DD, Gregorian (Thai Buddhist Era year minus 543)")
    account_number: str
    account_holder_latin: str = Field(description="Account holder name transliterated into Latin letters, never empty (e.g. Thai นางสาวนก ศรีสุวรรณ -> Nok Srisuwan, without titles)")
    transaction_reference: str = Field(description="Transaction or UPI reference; empty if none")
    amount: float = Field(description="Disputed amount as printed; 0 if none")
    legal_basis: str = Field(description="Law or section cited, as printed")
    action_requested: Literal["freeze_whole_account", "hold_specific_amount", "restrict_transactions", "other"]
    validity_days: int = Field(description="0 if not stated")
    document_language: str
    english_summary: str = Field(description="Two sentences, plain English")
    concerns: list[str] = Field(description="Anything in the document itself that is missing, inconsistent or unusual; empty if none")


class Quote(BaseModel):
    original: str
    english: str


class EvidenceItem(BaseModel):
    kind: Literal["chat_screenshot", "invoice", "courier_receipt", "listing", "payment_slip", "other"]
    platform: str
    counterparty_name_latin: str = Field(description="The other party (buyer or payer), from the chat header or the document, transliterated to Latin letters; empty if none")
    amount: float = Field(description="Sale or payment amount; 0 if none")
    date: str = Field(description="Date of the payment, or of the document for a receipt, YYYY-MM-DD Gregorian; empty if none")
    listing_date: str = Field(description="Date the item was listed for sale, YYYY-MM-DD; empty if not shown")
    item_description: str
    key_quotes: list[Quote] = Field(description="Up to 3 messages that matter most, original language plus English")
    tamper_signs: list[str] = Field(description="Visible signs of editing (mismatched fonts, misaligned text, cut-outs); empty if none")


class EvidencePack(BaseModel):
    items: list[EvidenceItem]
    transcript: str = Field(description="All chat message texts in order exactly as written, without names or times; empty if no chat")
    english_summary: str


class RedFlag(BaseModel):
    flag: str
    quote_original: str
    explanation: str


class ScamCheck(BaseModel):
    scam_type: str
    red_flags: list[RedFlag]
    amount_demanded: float
    destination: str
    english_summary: str


class Drafts(BaseModel):
    officer_summary: str = Field(description="3 to 5 sentences in English for the bank officer")
    authority_memo: str
    authority_memo_english: str
    customer_letter: str = Field(description="Empty string if the customer must not be notified")
    customer_letter_english: str
    citations_used: list[str] = Field(description="Rule ids used, e.g. IN-2")


class Approval(BaseModel):
    officer: str = "Duty officer"
    note: str = ""


READ_RULES = ("Copy names, numbers and references exactly as printed. Do not calculate or correct anything. "
              "Convert Thai Buddhist Era years to Gregorian by subtracting 543. Dates as YYYY-MM-DD.")


async def ask(parts, schema, system, models=READ):
    # a slow model must not hang the case: give up after 90 s and try the next one
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=0, http_options=types.HttpOptions(timeout=90_000),
                                      response_mime_type="application/json", response_schema=schema)
    for model in models:
        try:
            r = await client.aio.models.generate_content(model=model, contents=parts, config=cfg)
            if r.parsed is not None:
                return r.parsed
            print(f"gemini {model}: no structured result", flush=True)
        except Exception as e:  # busy/quota on one model: try the next, else tell the UI
            print(f"gemini {model} failed: {e}", flush=True)
    raise HTTPException(502, "The AI service is busy or over its limit. Please try again in a minute.")


async def read_upload(f: UploadFile):
    data = await f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"{f.filename}: file over 10 MB")
    mime = next((m for sig, m in MAGIC.items() if data.startswith(sig)), None)
    if not mime:
        raise HTTPException(415, f"{f.filename}: only PDF, PNG or JPEG accepted")
    return data, mime


def sample(name):
    return (HERE / "samples" / name).read_bytes(), "image/png"


def get_case(cid):
    if cid not in engine.CASES:
        raise HTTPException(404, "No such case")
    return engine.CASES[cid], STATE[cid]


def log(st, who, what):
    st["audit"].append({"at": datetime.now(SGT).isoformat(timespec="seconds"), "who": who, "what": what})
    save(next(k for k, v in STATE.items() if v is st))


def summary(cid):
    c, st = engine.CASES[cid], STATE[cid]
    r = engine.RULES[c["country"]]
    flagged = next((t for t in c["txns"] if t["id"] == c.get("flagged")), None)
    return {"id": cid, "country": c["country"], "kind": c["kind"], "name": c["customer"]["name"], "currency": r["currency"],
            "amount": flagged["amount"] if flagged else None, "status": st["status"],
            **engine.deadline(c, st["received"])}


# ---------- API ----------
@app.get("/api/cases")
def cases():
    return [summary(cid) for cid in engine.CASES]


@app.get("/api/cases/{cid}")
def case(cid: str):
    c, st = get_case(cid)
    out = {**summary(cid), "customer": c["customer"], "bank": c["bank"], "language": c["language"],
           "rules": engine.RULES[c["country"]], "requisition": c["requisition"], "audit": st["audit"], "request": st["request"], "running": st["running"],
           "live_steps": st["live_steps"], "agent_error": st["agent_error"],
           "evidence": st["evidence"], "drafts": st["drafts"], "decision": st["decision"]}
    if c["kind"] == "inbound":
        out["trace"] = engine.trace(c)
        out["signals"] = engine.signals(c)
    else:
        out["essentials"] = engine.essentials(c)
    return out


@app.post("/api/cases/{cid}/request")
async def check_request(cid: str, file: Optional[UploadFile] = File(None)):
    c, st = get_case(cid)
    data, mime = await read_upload(file) if file else sample(c["requisition"])
    req = await ask([types.Part.from_bytes(data=data, mime_type=mime),
                     "Read this freeze or restriction request sent to a bank."], Requisition,
                    "You read police, court and regulator requests to banks, in any language. " + READ_RULES)
    req = req.model_dump()
    st["request"] = {"extracted": req, "checks": engine.check_request(c, req)}
    st["status"] = "Request checked"
    log(st, "FairFreeze", "Request read and checked against the account and the rule pack")
    return st["request"]


@app.post("/api/cases/{cid}/evidence")
async def evidence(cid: str, files: list[UploadFile] = File(None), source: str = "officer"):
    c, st = get_case(cid)
    blobs = [await read_upload(f) for f in files] if files else [sample(n) for n in c["evidence"]]
    parts = [types.Part.from_bytes(data=d, mime_type=m) for d, m in blobs]
    if c["kind"] == "restriction":
        scam = await ask(parts + ["A bank customer under a scam restriction order shared this chat. Explain the scam signs."],
                         ScamCheck, "You are a scam-prevention specialist. Quote the chat exactly. Be kind; the customer is the victim.")
        st["evidence"] = {"source": source, "scam": scam.model_dump(), "strength": "victim", "plan": {
            "action": "restrict_with_essentials", "notify_customer": True,
            "why": "Keep the order, keep daily living money and regular bills open, block payments to the scammers."}}
    else:
        pack = await ask(parts + ["These documents were sent by a bank customer to show a frozen payment was a genuine sale."],
                         EvidencePack, "You read marketplace chats, receipts and payment slips in any language for a bank. " + READ_RULES)
        pack = pack.model_dump()
        items = [{**i, "counterparty_name": i["counterparty_name_latin"]} for i in pack["items"]]
        checks = engine.cross_check(c, items)
        sigs = engine.signals(c)
        hits = engine.reuse([engine.sha256(d) for d, _ in blobs], pack["transcript"], cid)
        level = engine.strength(checks, sigs, hits)
        st["evidence"] = {"source": source, "pack": pack, "checks": checks, "signals": sigs, "reuse": hits,
                          "strength": level, "plan": engine.plan(c, level)}
    st["status"] = "Evidence reviewed"
    log(st, "Customer" if source == "customer" else "FairFreeze", "Evidence received and checked")
    return st["evidence"]


def case_tools(cid, c, st):
    """Tools the Case Agent may call. Each returns facts computed by engine.py; the agent cannot change them."""
    def get_case_profile() -> dict:
        """Customer, bank, country, case type and the current status of the case."""
        return {"case": cid, "kind": c["kind"], "country": c["country"], "bank": c["bank"], "customer": c["customer"],
                "status": st["status"]}

    def get_freeze_request_check() -> dict:
        """What the police / court / regulator request says, and whether it matches the account and the law."""
        return st["request"] or {"note": "The request document has not been read yet."}

    def get_country_rules() -> dict:
        """The country rule pack: hold basis, deadlines, release authority, notice rule and rule texts with ids."""
        return engine.RULES[c["country"]]

    def trace_money() -> dict:
        """Lowest-intermediate-balance trace of the disputed credit: hold, release, traceable amount, next hops."""
        t = engine.trace(c)
        return {k: v for k, v in t.items() if k != "rows"}

    def behaviour_signals() -> list:
        """Facts from the account history that point towards an innocent holder or a mule."""
        return engine.signals(c)

    def get_evidence_review() -> dict:
        """The customer's documents as read, the cross-checks against the bank record, reuse hits, strength and plan."""
        return st["evidence"] or {"note": "No evidence yet."}

    def search_prior_cases(text: str) -> list:
        """Search other banks' past cases for the same story or script. Pass the chat text or a phrase."""
        return engine.reuse([], text, cid)

    def essentials_allowance() -> dict:
        """Singapore restriction orders: monthly living-money allowance, bills to keep paying, payees to block."""
        e = engine.essentials(c)
        return {k: v for k, v in e.items() if k != "rows"}

    common = [get_case_profile, get_freeze_request_check, get_country_rules, get_evidence_review]
    tools = common + ([trace_money, behaviour_signals, search_prior_cases] if c["kind"] == "inbound" else [essentials_allowance])

    def live(f):
        @functools.wraps(f)
        def w(*a, **k):
            st["live_steps"].append(f.__name__)
            return f(*a, **k)
        return w
    return [live(f) for f in tools]


async def run_agent(cid, c, st):
    """Gemini plans its own investigation: it decides which tools to call and in what order, then writes its assessment."""
    tools = case_tools(cid, c, st)
    cfg = types.GenerateContentConfig(
        tools=tools, temperature=0, http_options=types.HttpOptions(timeout=120_000),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=12),
        system_instruction="You are the FairFreeze Case Agent at a bank. Investigate this account freeze with your tools "
        "before concluding: verify the request, trace the money, weigh the evidence, look for scripts reused across banks, "
        "and check the country rules. Numbers come only from tools. Then write a short assessment for the officer: "
        "what happened, the recommended action and why, anything the automatic checks may have missed, and the rule ids "
        "that apply. You recommend; a human officer decides. Plain English, no em-dashes.")
    for model in REASON:
        try:
            r = await asyncio.to_thread(client.models.generate_content, model=model, config=cfg,
                                        contents=f"Investigate case {cid} and assess it.")
            steps = [{"tool": p.function_call.name, "args": dict(p.function_call.args or {})}
                     for h in (r.automatic_function_calling_history or []) for p in (h.parts or []) if p.function_call]
            return {"model": model, "steps": steps, "assessment": r.text or ""}
        except Exception as e:
            print(f"agent {model} failed: {e}", flush=True)
    raise HTTPException(502, "The AI service is busy or over its limit. Please try again in a minute.")


@app.post("/api/cases/{cid}/draft")
async def draft(cid: str):
    """Starts the Case Agent in the background (it takes ~45 s); the page polls the case for live steps and the result."""
    c, st = get_case(cid)
    if not st["evidence"]:
        raise HTTPException(409, "Review the evidence first")
    if st["running"]:
        return {"started": False, "running": True}
    st.update(running=True, live_steps=[], agent_error=None)
    TASKS.add(t := asyncio.create_task(draft_job(cid)))
    t.add_done_callback(TASKS.discard)
    return {"started": True}


TASKS = set()


async def draft_job(cid):
    c, st = get_case(cid)
    try:
        await draft_now(cid, c, st)
    except Exception as e:
        st["agent_error"] = getattr(e, "detail", None) or "The Case Agent could not finish. Please run it again."
        print(f"draft {cid} failed: {e}", flush=True)
    finally:
        st["running"] = False


async def draft_now(cid, c, st):
    r = engine.RULES[c["country"]]
    ev = st["evidence"]
    agent = await run_agent(cid, c, st)
    facts = {"case": cid, "bank": c["bank"], "customer": c["customer"], "currency": r["currency"],
             "request": (st["request"] or {}).get("extracted"), "plan": ev["plan"], "strength": ev["strength"],
             "rules": r["rules"], "notice_rule": r["notice_rule"], "release_authority": r["release_authority"],
             "agent_assessment": agent["assessment"]}
    if c["kind"] == "inbound":
        t = engine.trace(c)
        facts.update({k: t[k] for k in ("disputed", "balance", "hold", "release", "traceable", "next_hops",
                                         "index_whole_account", "index_fairfreeze")},
                     checks=ev["checks"], signals=ev["signals"], reuse=ev["reuse"])
    else:
        e = engine.essentials(c)
        facts.update({k: e[k] for k in ("allowance", "balance", "keep_paying", "blocked_payees", "already_sent", "blocked_pending")},
                     scam=ev["scam"])
    memo_lang = {"IN": "English", "TH": "Thai", "SG": "English"}[c["country"]]
    d = await ask([f"Case facts (computed, authoritative):\n{facts}"], Drafts, models=REASON, system=
                  f"You draft case papers for a bank officer handling an account freeze in {r['country']}. "
                  f"authority_memo goes to: {r['release_authority']}, written in {memo_lang}; authority_memo_english is its English text. "
                  f"customer_letter is in {c['language']}, warm, plain words, short sentences; customer_letter_english is its English text. "
                  f"If plan.notify_customer is false, customer_letter and customer_letter_english must be empty (tipping-off rule). "
                  f"Follow the notice rule: {r['notice_rule']} Use only the numbers given, never compute new ones; write amounts with the currency code. "
                  "Cite rule ids in square brackets like [IN-2]. Officer approval is still required; say the memo is a draft. No em-dashes.")
    drafts = d.model_dump()
    if not ev["plan"]["notify_customer"]:  # tipping-off rule is enforced in code, not left to the model
        drafts.update(customer_letter="", customer_letter_english="")
    st["drafts"] = {**drafts, "agent": agent}
    st["status"] = "Drafts ready"
    log(st, f"Case Agent ({agent['model']})", f"Investigated with {len(agent['steps'])} tool calls; memo and letter drafted")


@app.post("/api/cases/{cid}/approve")
def approve(cid: str, a: Approval):
    c, st = get_case(cid)
    if not st["drafts"]:
        raise HTTPException(409, "Draft the papers first")
    st["decision"] = {**st["evidence"]["plan"], "officer": a.officer[:60], "note": a.note[:500],
                      "at": datetime.now(SGT).isoformat(timespec="minutes")}
    st["status"] = "Approved"
    log(st, a.officer[:60], f"Approved: {st['evidence']['plan']['action']}")
    return st["decision"]


@app.post("/api/reset")
def reset():
    """Puts every demo case back to New (evaluators share one deployment)."""
    for cid in STATE:
        if not STATE[cid]["running"]:
            STATE[cid] = fresh(cid)
            save(cid)
    return {"reset": True}


@app.get("/api/public/{cid}")
def public(cid: str):
    """What the customer's link may show: amounts and deadline only, never the investigation (tipping-off rule)."""
    c, st = get_case(cid)
    r = engine.RULES[c["country"]]
    out = {"id": cid, "kind": c["kind"], "first_name": c["customer"]["name"].split()[0], "language": c["language"],
           "currency": r["currency"], "bank": c["bank"], "deadline_label": r["deadline_label"],
           "submitted": bool(st["evidence"] and st["evidence"]["source"] == "customer")}
    if c["kind"] == "inbound":
        t = engine.trace(c)
        out.update(held=t["hold"], free=t["release"])
    else:
        e = engine.essentials(c)
        out.update(allowance=e["allowance"], keep_paying=e["keep_paying"])
    return out


@app.get("/api/regulator")
def regulator():
    out = []
    for cid, c in engine.CASES.items():
        if c["kind"] != "inbound":
            continue
        t, st = engine.trace(c), STATE[cid]
        out.append({"id": cid, "country": c["country"], "bank": c["bank"], "disputed": t["disputed"],
                    "requested_hold": t["balance"], "fairfreeze_hold": (st["decision"] or {}).get("hold", t["hold"]),
                    "index_requested": t["index_whole_account"],
                    "index_fairfreeze": round((st["decision"] or {}).get("hold", t["hold"]) / t["disputed"], 2),
                    "status": st["status"]})
    return out


@app.get("/samples/{name}")
def get_sample(name: str):
    if name not in SAMPLES:
        raise HTTPException(404)
    return FileResponse(HERE / "samples" / name, media_type="image/png")


@app.get("/logo.svg")
def logo():
    return FileResponse(HERE / "static" / "logo.svg", media_type="image/svg+xml")


@app.get("/")
@app.get("/c/{cid}")
def index(cid: str = ""):
    return FileResponse(HERE / "static" / "index.html")
