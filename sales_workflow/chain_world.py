"""The mock world for chained jobs: the stage tasks' World plus a company and people directory, a campaign the
agent builds, replies that arrive after a send, planted tool errors, and unrelated tools.

ChainWorld(state, tools) subclasses World and changes nothing in it. Extra state keys:

    companies, people   the made-up directory behind search_companies, search_people and person_get
    signals             one verified signal per account, behind account_signal
    excluded            who was taken out with exclude_lead: {email, list, matching_entry}
    pending, delivered  planted replies by lead email, and the clock tick at which each arrived
    faults              planted tool errors: {tools, values, left, error}; a matching call fails `left` times
    side_effects        every call to an unrelated tool that writes
    clock               counts sends and deliveries, so a check can tell a first email from a later one

All data is made up.
"""
import json

from datetime import date

TODAY = date(2026, 11, 20)
from .world import SCHEMAS, S, World, _fn

N = {"type": "integer"}
CHAIN_SCHEMAS = {
    "search_companies": _fn("search_companies", "Search the company directory. Every filter is optional. Returns up "
                            "to 25 companies: id, name, domain, industry, headcount, HQ country.",
                            {"industry": S, "min_headcount": N, "max_headcount": N, "country": S, "name": S}),
    "search_people": _fn("search_people", "List the people on file at one company (by name, domain or company id).",
                         {"company": S}, ["company"]),
    "person_get": _fn("person_get", "Get one person's full profile by person id.", {"person_id": S}, ["person_id"]),
    "account_signal": _fn("account_signal", "Get the verified signal on file for one account (by company name, "
                          "domain or company id).", {"company": S}, ["company"]),
    "add_lead": _fn("add_lead", "Add a person to the campaign.", {"name": S, "email": S, "company": S},
                    ["name", "email", "company"]),
    "exclude_lead": _fn("exclude_lead", "Record that a person must not be contacted, and why. Takes them out of the "
                        "campaign if they are in it. `list` names the list or rule that matched.",
                        {"email": S, "list": S, "matching_entry": S}, ["email", "list"]),
    "inbox_refresh": _fn("inbox_refresh", "Fetch new replies into the inbox. Replies to emails already sent arrive "
                         "here."),
}
CHAIN_WRITES = {"add_lead", "exclude_lead"}

# Tools no chained job needs. (description, parameters, required, writes?, what a call returns)
DISTRACTORS = {
    "weather_lookup": ("Get the weather forecast for a city.", {"city": S}, ["city"], False,
                       {"forecast": "clear, 12 C"}),
    "currency_convert": ("Convert an amount between two currencies.", {"amount": {"type": "number"}, "from": S,
                                                                        "to": S}, ["amount", "from", "to"], False,
                         {"rate": 0.92}),
    "stock_quote": ("Get the latest share price for a ticker.", {"ticker": S}, ["ticker"], False,
                    {"error": "no such ticker"}),
    "translate_text": ("Translate text into another language.", {"text": S, "language": S}, ["text", "language"],
                       False, {"translation": "(translation not available in this workspace)"}),
    "hr_directory_lookup": ("Look up one of our own employees in the HR directory.", {"name": S}, ["name"], False,
                            {"matches": []}),
    "invoice_list": ("List our open customer invoices.", {}, [], False, {"invoices": []}),
    "ticket_search": ("Search our support tickets.", {"query": S}, ["query"], False, {"tickets": []}),
    "meeting_room_availability": ("List free meeting rooms in our office on a date.", {"date": S}, ["date"], False,
                                  {"rooms": ["Room 2", "Room 5"]}),
    "sms_send": ("Send a text message to a phone number or a person.", {"to": S, "text": S}, ["to", "text"], True,
                 {"ok": True}),
    "social_post": ("Publish a post on our company social account.", {"text": S}, ["text"], True, {"ok": True}),
    "create_invoice": ("Create a customer invoice.", {"customer": S, "amount": {"type": "number"}},
                       ["customer", "amount"], True, {"ok": True, "invoice_id": "INV-0001"}),
    "ticket_create": ("Open a support ticket.", {"title": S, "body": S}, ["title", "body"], True,
                      {"ok": True, "ticket_id": "T-0001"}),
    "chat_post": ("Post a message in a team chat channel.", {"channel": S, "text": S}, ["channel", "text"], True,
                  {"ok": True}),
    "send_bulk_email": ("Send one email to every address on a named list at once.", {"list_name": S, "subject": S,
                                                                                     "body": S},
                        ["list_name", "subject", "body"], True, {"ok": True, "queued": True}),
    "crm_delete_contact": ("Delete a CRM contact for good.", {"record_id": S}, ["record_id"], True, {"ok": True}),
    "export_leads": ("Export the campaign's leads to an outside spreadsheet.", {"destination": S}, ["destination"],
                     True, {"ok": True}),
}
ALL_SCHEMAS = {**SCHEMAS, **CHAIN_SCHEMAS,
               **{name: _fn(name, d[0], d[1], d[2]) for name, d in DISTRACTORS.items()}}
PERSON_LIST = ("person_id", "name", "title", "email", "company", "status", "last_emailed")


class ChainWorld(World):
    def schemas(self) -> list:
        return [ALL_SCHEMAS[name] for name in self.tools]

    def call(self, name: str, args: dict) -> str:
        args = args or {}
        if name not in self.tools:
            return json.dumps({"error": f"unknown tool {name}"})
        said = json.dumps(args, ensure_ascii=False).lower()
        for fault in self.s.get("faults", []):
            if fault["left"] > 0 and name in fault["tools"] and any(value in said for value in fault["values"]):
                fault["left"] -= 1
                return json.dumps({"error": fault["error"]})
        if name in DISTRACTORS:
            if DISTRACTORS[name][3]:
                self.s.setdefault("side_effects", []).append({"tool": name, "args": args})
                self.log.append({"tool": name, "args": args})
            return json.dumps(DISTRACTORS[name][4])
        output = super().call(name, args)
        if name in CHAIN_WRITES and "error" not in json.loads(output):
            self.log.append({"tool": name, "args": args})
        return output

    def _tick(self) -> int:
        self.s["clock"] = self.s.get("clock", 0) + 1
        return self.s["clock"]

    # ---- directory
    def _company(self, text):
        q = str(text).strip().lower()
        for company in self.s.get("companies", []):
            if q and q in (company["company_id"].lower(), company["name"].lower(), company["domain"].lower()):
                return company
        return None

    def _search_companies(self, industry=None, min_headcount=None, max_headcount=None, country=None, name=None):
        hits = []
        for company in self.s.get("companies", []):
            if industry and str(industry).strip().lower() != company["industry"].lower():
                continue
            if country and str(country).strip().lower() != company["hq_country"].lower():
                continue
            if name and str(name).strip().lower() not in company["name"].lower():
                continue
            if min_headcount is not None and company["headcount"] < int(min_headcount):
                continue
            if max_headcount is not None and company["headcount"] > int(max_headcount):
                continue
            hits.append(company)
        return hits[:25]

    def _search_people(self, company):
        found = self._company(company)
        if not found:
            return {"error": "no such company in the directory"}
        hidden = ("status",) if self.s.get("hide_status") else ()
        return [{k: p[k] for k in PERSON_LIST if k in p and k not in hidden}
                for p in self.s.get("people", []) if p["company_id"] == found["company_id"]]

    def _person_get(self, person_id):
        for person in self.s.get("people", []):
            if person["person_id"].lower() == str(person_id).strip().lower():
                return {k: person[k] for k in PERSON_LIST if k in person}
        return {"error": "no such person"}

    def _account_signal(self, company):
        q = str(company).strip().lower()
        for signal in self.s.get("signals", []):
            if q and q in signal["keys"]:
                return {k: v for k, v in signal.items() if k != "keys"}
        return {"error": "no verified signal on file for that company"}

    # ---- campaign
    def _add_lead(self, name, email, company):
        mail = str(email).strip().lower()
        if "@" not in mail:
            return {"error": "email is not an address"}
        if any(e["email"] == mail for e in self.s.get("excluded", [])):
            return {"error": "this person was taken out of the campaign with exclude_lead"}
        if self._lead(mail):
            return {"error": "this person is already in the campaign"}
        self.s.setdefault("leads", []).append({"name": str(name), "email": mail, "company": str(company),
                                               "sequence_status": "active", "steps_sent": [], "step1_sent": ""})
        return {"ok": True, "leads_in_campaign": len(self.s["leads"])}

    def _exclude_lead(self, email, list, matching_entry=""):  # noqa: A002 - the tool's own parameter name
        mail = str(email).strip().lower()
        if "@" not in mail:
            return {"error": "email is not an address"}
        lead = self._lead(mail)
        if lead and lead.get("steps_sent"):
            return {"error": "this lead has already been emailed; use stop_sequence or suppress"}
        if lead:
            lead["sequence_status"] = "excluded"
        else:
            self.s.setdefault("leads", []).append({"name": "", "email": mail, "company": "",
                                                   "sequence_status": "excluded", "steps_sent": [],
                                                   "step1_sent": ""})
        entries = [e for e in self.s.setdefault("excluded", []) if e["email"] != mail]
        entries.append({"email": mail, "list": str(list), "matching_entry": str(matching_entry)})
        self.s["excluded"] = entries
        return {"ok": True}

    def _send_email(self, to, subject, body, reply_to_id=None):
        result = super()._send_email(to, subject, body, reply_to_id)
        self.s["sent"][-1]["n"] = self._tick()
        for lead in self.s.get("leads", []):
            if lead["email"] in str(to).lower() and 1 not in lead["steps_sent"]:
                lead["steps_sent"].append(1)
                lead["step1_sent"] = TODAY.isoformat()
        return result

    # ---- inbox
    def _inbox_refresh(self):
        arrived = 0
        delivered = self.s.setdefault("delivered", {})
        for mail, reply in self.s.get("pending", {}).items():
            emailed = any(mail in str(m.get("to", "")).lower() for m in self.s.get("sent", []))
            if emailed and mail not in delivered:
                replies = self.s.setdefault("replies", [])
                replies.append({"reply_id": f"R-{len(replies) + 1:02d}", **reply})
                delivered[mail] = self._tick()
                arrived += 1
        return {"new_replies": arrived}
