"""
AI Chatbot — answers questions about tenders in the database, and anything
else the CEO asks, using tool-calling. Primary model is OpenAI's
gpt-4o-mini; if OpenAI can't be reached (network issue, outage, rate
limit), it falls back to DeepSeek — a different provider entirely, so a
problem with one service doesn't take the chatbot down.

The model never gets raw SQL access. It can only call a fixed set of
read-only query functions (see database/db.py's "Read-only lookups for the
AI chatbot's tool-calling" section) — so it can answer basically any
question about the data, but nothing it does can modify or delete
anything. Questions that don't match any tool are just answered from the
model's own general knowledge, the same as talking to it normally.
"""

import json
import os

import requests
from dotenv import load_dotenv

from datetime import date, timedelta

from database.db import (
    get_all_tenders, get_stats, get_relevant_tenders_list,
    get_tenders_by_date, search_tenders_by_text, get_tender_by_number,
    get_company_turnover_list, get_company_credentials_list,
    get_partners_list, get_partner_credentials_list,
    get_project_references_list, get_project_quantities_list,
    get_all_users, get_relevant_keywords_list, get_all_ceo_searches,
    get_kppp_criteria_list, get_learned_patterns,
)

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

def _system_prompt(is_admin: bool) -> str:
    admin_note = (
        "You also have tools for the company's own Admin-section data: "
        "turnover by financial year, certificates/credentials (ISO etc.), "
        "partners and their credentials, past project references, project "
        "quantities executed, and the app's user accounts. Use them for any "
        "question about what the company has, holds, has done, or earned - "
        "never say you lack access to this data; call the tool."
        if is_admin else
        "The company's Admin-section data (turnover, certificates, partners, "
        "project references, users) is restricted to admin accounts, and "
        "this user is not an admin - if asked, say it is admin-only."
    )
    _today = date.today()
    # Models are unreliable at doing weekday arithmetic themselves (e.g.
    # "last Thursday" computed as the wrong date) - so instead of asking it
    # to calculate, just hand it the actual date for every weekday in the
    # last 7 days as a lookup table it can read off directly.
    _recent = ", ".join(
        f"{(_today - timedelta(days=n)).strftime('%A')} = "
        f"{(_today - timedelta(days=n)).isoformat()}"
        + (" (today)" if n == 0 else "")
        for n in range(7)
    )
    return (
        f"Today's date is {_today.isoformat()} ({_today.strftime('%A')}). "
        f"The last 7 days, so you never have to calculate a weekday's date "
        f"yourself - just read it off this list: {_recent}. You "
        "are the Tender Intelligence assistant for Chipscape, a CCTV/security "
        "systems company that bids on government tenders tracked from the "
        "GeM and KPPP (Karnataka) portals. Use the provided tools to answer "
        "any question about the app's data - tenders (new, by date, by "
        "topic, relevant list, details, stats) and the search setup (saved "
        "keywords, past searches, KPPP filters). " + admin_note + " "
        "SEARCHING: the search tool matches literal words in a tender's "
        "title/department/bid number, but people phrase things differently "
        "from the tender text. So use your own knowledge to expand what the "
        "user says into several likely alternatives and pass them ALL in one "
        "search call (e.g. 'body cam' -> 'body worn camera', 'bodycam', "
        "'BWC'; 'CCTV' -> 'surveillance camera', 'IP camera'). If a search "
        "returns nothing, retry with different wording before concluding "
        "there are none. Resolve relative dates ('today', 'yesterday', 'this "
        "week') into real dates from today's date, ALWAYS in the current "
        "year given above - never a past year like 2023 just because that's "
        "older training data; today's actual year is the one stated above. "
        "A bare day-of-month with no month said ('the 7th', 'on the 15th') "
        "means the most recent occurrence of that date up to and including "
        "today: this month if that day number is <= today's day-of-month, "
        "otherwise last month. A bare weekday name with no 'next' ('Friday', "
        "'on Monday') means the most recent past occurrence of that weekday "
        "(last Friday), not today even if today happens to be that weekday "
        "unless the user says 'today' - read its exact date off the last-7-"
        "days list above rather than counting backwards yourself, that list "
        "already covers every weekday at least once. When reporting counts, "
        "use the tool's total_matches, not the number of rows shown. "
        "Whenever you list or count tenders that span more than one portal "
        "(e.g. a date-based or keyword search covering both GeM and KPPP), "
        "open with a one-line breakdown by portal before the list, like "
        "'6 on GeM, 2 on KPPP (8 total)' - do this even when one portal's "
        "count is 0. "
        "If a question is not about the app's data, answer normally from "
        "your own knowledge, and you may combine your knowledge with the "
        "data (e.g. advise which certifications to add given the ones held). "
        "Keep answers concise and formatted for a compact chat panel, and "
        "avoid large headings. Whenever you show actual tender details (not "
        "just a title or count), use a markdown table, never bullet points "
        "or a paragraph - columns: Tender Number, Title, Department, Start "
        "Date, Deadline. If the tenders span more than one portal, show a "
        "SEPARATE table per portal (one table for GeM, one for KPPP, each "
        "under its own short 'GeM' / 'KPPP' heading) instead of one mixed "
        "table or list - this is so the CEO can tell the two portals apart "
        "at a glance. "
        "When a tender list has more results than you can reasonably list "
        "inline, show just the first few per portal and then simply stop - "
        "never write filler like '...and X more' or 'additional entries "
        "truncated', the app itself already adds a 'View all N tenders' "
        "button under your reply whenever there are more than you showed."
    )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_tenders_by_date",
            "description": "Get tenders the agent found on a specific date (YYYY-MM-DD).",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "portal": {"type": "string", "enum": ["GeM", "KPPP"]},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tenders",
            "description": (
                "Search tenders. `keywords` is a list of ALTERNATIVE search "
                "phrases (a tender matches if any one matches; within one "
                "phrase every word must appear in the title, department or "
                "bid number). Include synonyms/related wording you can think "
                "of. All filters are optional and combine: keywords may be "
                "omitted to list by date/portal/status alone. Returns "
                "total_matches plus up to 30 of the newest matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "portal": {"type": "string", "enum": ["GeM", "KPPP"]},
                    "date_from": {"type": "string", "description": "Earliest day the agent found it, YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "Latest day the agent found it, YYYY-MM-DD"},
                    "status": {"type": "string", "enum": ["active", "new", "saved", "expired"],
                               "description": "'active' = everything not expired"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_relevant_tenders",
            "description": "Get tenders matched against the saved 'Relevant Tenders' keyword list.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_new_tenders",
            "description": "Get today's newly found tenders.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tender_details",
            "description": "Get full details of one tender by its bid/tender number.",
            "parameters": {
                "type": "object",
                "properties": {"tender_number": {"type": "string"}},
                "required": ["tender_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": "Get overall counts: total tenders, new today, saved, expired, relevant, searches run, patterns learned.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_turnover",
            "description": "Admin data: Chipscape's turnover by financial year (with GST total and audit status).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_credentials",
            "description": "Admin data: Chipscape's certificates and credentials (ISO, OEM authorizations, registrations etc.) with issuer, certificate number, issue/expiry dates and status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_partners",
            "description": "Admin data: partner/subcontractor companies and each partner's own credentials.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_references",
            "description": "Admin data: past projects Chipscape has done (customer, location, scope, value, year, status) used as references for tender eligibility.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_quantities",
            "description": "Admin data: quantities executed on past projects (e.g. number of cameras installed) by solution category.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_users",
            "description": "Admin data: user accounts of this app (usernames, roles, last active). Never includes passwords.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_search_setup",
            "description": "The agent's search setup: saved Relevant-Tenders keywords, past keyword searches, KPPP search filters, and learned keyword patterns.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


ADMIN_ONLY_TOOLS = {
    "get_company_turnover", "get_company_credentials", "get_partners",
    "get_project_references", "get_project_quantities", "get_users",
}


def _tools_for(is_admin: bool) -> list:
    if is_admin:
        return TOOLS
    return [t for t in TOOLS if t["function"]["name"] not in ADMIN_ONLY_TOOLS]


def _clean(rows, drop=("created_at", "updated_at", "last_updated")):
    return [{k: v for k, v in dict(r).items() if k not in drop} for r in rows]


def _summarize_tender(t: dict) -> dict:
    return {
        "tender_number": t.get("tender_number"),
        "title": (t.get("title") or "")[:150],
        "portal": t.get("portal"),
        "department": t.get("department"),
        "start_date": str(t.get("start_date_parsed") or ""),
        "deadline": t.get("deadline"),
        "status": t.get("status"),
        "value": t.get("value"),
        "found_at": t.get("found_at"),
    }


def _dedupe_tenders(tenders):
    """Separate tool calls in the same turn (e.g. GeM + KPPP fetched
    separately, or a retry with different search synonyms) can surface the
    same tender more than once in the sidecar list; keep first occurrence
    only, same order."""
    if not tenders:
        return tenders
    seen = set()
    out = []
    for t in tenders:
        key = t.get("tender_number")
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _sidecar_put(sidecar, name, args, rows):
    """Records a tool call's full tender list under a key of (tool name,
    portal). Same key as a PREVIOUS call this turn overwrites rather than
    appends — that's deliberately the case for e.g. search_tenders retried
    with different synonym wording after an empty first try, where only the
    final, successful attempt's results should count. A DIFFERENT key (most
    commonly the same tool called once per portal, e.g. GeM then KPPP, which
    are complementary rather than retries of each other) accumulates
    instead, so both portals' results end up in the final list together."""
    if sidecar is None:
        return
    key = (name, args.get("portal"))
    sidecar.setdefault("by_key", {})[key] = [_summarize_tender(t) for t in rows]


def _run_tool(name: str, args: dict, is_admin: bool = False, sidecar: dict = None) -> str:
    """sidecar, if given, is a dict this fills in with the FULL (uncapped)
    tender list from whichever tool call actually fetched tenders — the
    JSON handed back to the model stays capped (to keep the prompt small),
    but the UI wants every row so "View all N tenders" can expand to the
    real full list instead of just the handful the model chose to describe
    in its own prose."""
    try:
        if name in ADMIN_ONLY_TOOLS and not is_admin:
            return json.dumps({"error": "This data is restricted to admin accounts."})
        if name == "get_tenders_by_date":
            rows = get_tenders_by_date(args["date"], args.get("portal"))
            _sidecar_put(sidecar, name, args, rows)
            return json.dumps({"total_matches": len(rows), "tenders": [_summarize_tender(t) for t in rows[:30]]})
        if name == "search_tenders":
            # limit=200 here (not the usual 30) so the sidecar below can
            # carry the real full result set for "View all" — the JSON sent
            # to the model is still sliced down to 30 rows right after, to
            # keep its own prompt small.
            rows, total = search_tenders_by_text(
                args.get("keywords") or args.get("keyword"), args.get("portal"),
                date_from=args.get("date_from"), date_to=args.get("date_to"),
                status=args.get("status"), limit=200,
            )
            _sidecar_put(sidecar, name, args, rows)
            return json.dumps({"total_matches": total, "showing": min(len(rows), 30),
                               "tenders": [_summarize_tender(t) for t in rows[:30]]})
        if name == "get_relevant_tenders":
            rows = get_relevant_tenders_list()
            _sidecar_put(sidecar, name, args, rows)
            return json.dumps({"total_matches": len(rows), "tenders": [_summarize_tender(t) for t in rows[:30]]})
        if name == "get_new_tenders":
            rows = get_all_tenders(status_filter="new")
            _sidecar_put(sidecar, name, args, rows)
            return json.dumps({"total_matches": len(rows), "tenders": [_summarize_tender(t) for t in rows[:30]]})
        if name == "get_tender_details":
            t = get_tender_by_number(args["tender_number"])
            return json.dumps(_summarize_tender(t) if t else {"error": "not found"})
        if name == "get_stats":
            return json.dumps(get_stats())
        if name == "get_company_turnover":
            return json.dumps(_clean(get_company_turnover_list()), default=str)
        if name == "get_company_credentials":
            return json.dumps(_clean(get_company_credentials_list()), default=str)
        if name == "get_partners":
            return json.dumps({"partners": _clean(get_partners_list()),
                               "partner_credentials": _clean(get_partner_credentials_list())}, default=str)
        if name == "get_project_references":
            return json.dumps(_clean(get_project_references_list()), default=str)
        if name == "get_project_quantities":
            return json.dumps(_clean(get_project_quantities_list()), default=str)
        if name == "get_users":
            return json.dumps(_clean(get_all_users(), drop=("id",)), default=str)
        if name == "get_search_setup":
            return json.dumps({
                "relevant_keywords": [k["keyword"] for k in get_relevant_keywords_list()],
                "past_searches": _clean(get_all_ceo_searches(), drop=("id",)),
                "kppp_filters": _clean(get_kppp_criteria_list(), drop=("id", "created_at")),
                "learned_patterns": _clean(get_learned_patterns(), drop=("id",)),
            }, default=str)
        return json.dumps({"error": f"unknown tool '{name}'"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _call_llm(provider: str, messages: list, tools: list) -> dict:
    """Makes one chat-completions call against the given provider ('openai'
    or 'deepseek') and returns the response message. Raises on any
    network/HTTP/parsing failure — callers decide what to do about it."""
    if provider == "openai":
        url, key, model = OPENAI_URL, OPENAI_API_KEY, OPENAI_MODEL
    else:
        url, key, model = DEEPSEEK_URL, DEEPSEEK_API_KEY, DEEPSEEK_MODEL

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "tools": tools,
            "max_tokens": 1000,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def ask(history: list, user_message: str, user_role: str = "regular"):
    """
    history: prior turns in this chat session, as a flat list of
    {"role": "user"/"assistant", "content": str} — only the final answer of
    each past turn is kept (not its tool-call plumbing), since every turn
    resolves its own tool calls fully before returning.

    Returns (reply_text, tenders) — tenders is the full, uncapped list of
    tenders from whichever tool call (if any) actually fetched tenders this
    turn, for the UI's "View all N tenders" expand button; None if this
    turn's answer didn't involve a tender-fetching tool at all.
    """
    is_admin = user_role == "admin"
    sidecar = {}
    tools = _tools_for(is_admin)
    messages = [{"role": "system", "content": _system_prompt(is_admin)}]
    # Only role/content go to the API — a history entry may carry extra UI-
    # only keys (e.g. "tenders", for the "View all" expand button), which
    # the chat-completions endpoints don't accept on a message object.
    messages += [{"role": h["role"], "content": h["content"]} for h in history[-10:]]
    messages.append({"role": "user", "content": user_message})

    # Primary is OpenAI (gpt-4o-mini); once a call to it fails for any
    # reason, every remaining round-trip in this same answer switches to
    # DeepSeek too, rather than retrying OpenAI each time — a mid-outage
    # provider rarely recovers within the same request.
    provider = "openai"

    for _ in range(6):  # cap tool round-trips so a confused model can't loop forever
        try:
            choice = _call_llm(provider, messages, tools)
        except Exception as primary_err:
            if provider == "openai":
                provider = "deepseek"
                try:
                    choice = _call_llm(provider, messages, tools)
                except Exception as fallback_err:
                    return f"Sorry, I couldn't reach the AI service right now ({fallback_err}).", None
            else:
                return f"Sorry, I couldn't reach the AI service right now ({primary_err}).", None

        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            reply = choice.get("content") or "I didn't get a response — try rephrasing that."
            flat = [t for rows in sidecar.get("by_key", {}).values() for t in rows]
            return reply, _dedupe_tenders(flat)

        messages.append(choice)
        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                fn_args = json.loads(call["function"]["arguments"] or "{}")
            except Exception:
                fn_args = {}
            result = _run_tool(fn_name, fn_args, is_admin, sidecar)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    return "That took too many steps to answer — try asking something more specific.", None
