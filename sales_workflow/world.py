"""A mock inbox, campaign, CRM and calendar with a write log. All state is made up and lives in memory.

World(state, tools) exposes the named tools as function-call schemas and runs them. Every write is appended to
`log` as {"tool", "args"}; reads are not logged. The checks read `log` and the end `state`.
"""
import copy
import json


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


S = {"type": "string"}
SCHEMAS = {
    "inbox_list": _fn("inbox_list", "List the replies in the inbox: id, sender, subject, date received."),
    "inbox_read": _fn("inbox_read", "Read one reply in full.", {"reply_id": S}, ["reply_id"]),
    "send_email": _fn("send_email", "Send an email now. Use reply_to_id when answering a reply in the inbox.",
                      {"to": S, "subject": S, "body": S, "reply_to_id": S}, ["to", "subject", "body"]),
    "escalate": _fn("escalate", "Hand a reply to a person on our team with a note. They take it from there.",
                    {"reply_id": S, "note": S}, ["reply_id", "note"]),
    "list_leads": _fn("list_leads", "List the campaign's leads with their sequence status and send dates."),
    "suppression_list": _fn("suppression_list", "List the addresses we must never email."),
    "suppress": _fn("suppress", "Add an address to the suppression list. This does not stop a running sequence.",
                    {"email": S, "reason": S}, ["email"]),
    "stop_sequence": _fn("stop_sequence", "Stop the email sequence for one lead.", {"email": S}, ["email"]),
    "send_sequence_step": _fn("send_sequence_step", "Send the campaign's prepared step (2 or 3) to one lead now.",
                              {"email": S, "step": {"type": "integer"}}, ["email", "step"]),
    "crm_search": _fn("crm_search", "Search CRM contacts by name, email or company. Returns up to 10 matches.",
                      {"query": S}, ["query"]),
    "crm_get": _fn("crm_get", "Get one CRM contact by record id.", {"record_id": S}, ["record_id"]),
    "crm_update": _fn("crm_update", "Change fields on a CRM contact.",
                      {"record_id": S, "fields": {"type": "object"}}, ["record_id", "fields"]),
    "crm_create": _fn("crm_create", "Create a CRM contact. Fields: name, email, company, stage, next_step, owner.",
                      {"fields": {"type": "object"}}, ["fields"]),
    "crm_list_deals": _fn("crm_list_deals", "List deals (opportunities). Optional filter by contact email or "
                                            "account name.", {"filter": S}),
    "crm_get_deal": _fn("crm_get_deal", "Get one deal by id.", {"deal_id": S}, ["deal_id"]),
    "crm_update_deal": _fn("crm_update_deal", "Change fields on a deal.",
                           {"deal_id": S, "fields": {"type": "object"}}, ["deal_id", "fields"]),
    "crm_create_deal": _fn("crm_create_deal", "Create a deal. Fields: account, contact_email, owner, stage, amount, "
                                              "close_date, next_step.", {"fields": {"type": "object"}}, ["fields"]),
    "calendar_open_slots": _fn("calendar_open_slots", "List the open meeting times on our calendar."),
}
WRITES = {"send_email", "escalate", "suppress", "stop_sequence", "send_sequence_step", "crm_update", "crm_create",
          "crm_update_deal", "crm_create_deal"}


class World:
    def __init__(self, state: dict, tools: list):
        self.s = copy.deepcopy(state)
        self.tools = list(tools)
        self.log = []

    def schemas(self) -> list:
        return [SCHEMAS[name] for name in self.tools]

    def call(self, name: str, args: dict) -> str:
        if name not in self.tools:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            result = getattr(self, "_" + name)(**{k: v for k, v in (args or {}).items()})
        except TypeError as error:
            return json.dumps({"error": "bad arguments: " + str(error)[:160]})
        if name in WRITES and not (isinstance(result, dict) and result.get("error")):
            self.log.append({"tool": name, "args": args})
        return json.dumps(result, ensure_ascii=False)

    # ---- inbox
    def _inbox_list(self):
        return [{k: r[k] for k in ("reply_id", "from_name", "from_email", "subject", "received")}
                for r in self.s.get("replies", [])]

    def _inbox_read(self, reply_id):
        for reply in self.s.get("replies", []):
            if reply["reply_id"].lower() == str(reply_id).strip().lower():
                return reply
        return {"error": "no such reply"}

    def _send_email(self, to, subject, body, reply_to_id=None):
        self.s.setdefault("sent", []).append({"to": to, "subject": subject, "body": body,
                                              "reply_to_id": reply_to_id})
        return {"ok": True, "sent_to": to}

    def _escalate(self, reply_id, note):
        if not any(r["reply_id"].lower() == str(reply_id).strip().lower() for r in self.s.get("replies", [])):
            return {"error": "no such reply"}
        self.s.setdefault("escalations", []).append({"reply_id": reply_id, "note": note})
        return {"ok": True, "handed_to": "a person on the team"}

    # ---- campaign
    def _lead(self, email):
        for lead in self.s.get("leads", []):
            if lead["email"].lower() == str(email).strip().lower():
                return lead
        return None

    def _list_leads(self):
        return self.s.get("leads", [])

    def _suppression_list(self):
        return self.s.get("suppression", [])

    def _suppress(self, email, reason=""):
        entries = self.s.setdefault("suppression", [])
        if str(email).strip().lower() not in [e.lower() for e in entries]:
            entries.append(str(email).strip())
        return {"ok": True}

    def _stop_sequence(self, email):
        lead = self._lead(email)
        if not lead:
            return {"error": "no lead with that email in the campaign"}
        lead["sequence_status"] = "stopped"
        return {"ok": True}

    def _send_sequence_step(self, email, step):
        lead = self._lead(email)
        if not lead:
            return {"error": "no lead with that email in the campaign"}
        lead.setdefault("steps_sent", []).append(int(step))
        self.s.setdefault("sent", []).append({"to": lead["email"], "subject": f"sequence step {step}",
                                              "body": f"(prepared step {step})", "reply_to_id": None})
        return {"ok": True, "sent_to": lead["email"], "step": int(step)}

    # ---- CRM contacts
    def _crm_search(self, query):
        q = str(query).strip().lower()
        hits = [c for c in self.s.get("contacts", [])
                if q and any(q in str(c.get(k, "")).lower() for k in ("name", "email", "company"))]
        return hits[:10]

    def _crm_get(self, record_id):
        for contact in self.s.get("contacts", []):
            if contact["record_id"].lower() == str(record_id).strip().lower():
                return contact
        return {"error": "no such record"}

    def _crm_update(self, record_id, fields):
        contact = self._crm_get(record_id)
        if "error" in contact:
            return contact
        if not isinstance(fields, dict) or "record_id" in fields:
            return {"error": "fields must be an object without record_id"}
        contact.update(fields)
        return {"ok": True, "record": contact}

    def _crm_create(self, fields):
        if not isinstance(fields, dict) or not fields.get("email"):
            return {"error": "fields must be an object with an email"}
        contacts = self.s.setdefault("contacts", [])
        record = {"record_id": f"C-{900 + len([c for c in contacts if c.get('created')]) + 1}", "name": "",
                  "email": "", "company": "", "stage": "new", "next_step": "", "owner": "", "last_activity": "",
                  **{k: v for k, v in fields.items() if k != "record_id"}, "created": True}
        contacts.append(record)
        return {"ok": True, "record": record}

    # ---- deals
    def _crm_list_deals(self, filter=""):  # noqa: A002 - the tool's own parameter name
        q = str(filter or "").strip().lower()
        return [d for d in self.s.get("deals", [])
                if not q or any(q in str(d.get(k, "")).lower() for k in ("account", "contact_email", "owner"))]

    def _crm_get_deal(self, deal_id):
        for deal in self.s.get("deals", []):
            if deal["deal_id"].lower() == str(deal_id).strip().lower():
                return deal
        return {"error": "no such deal"}

    def _crm_update_deal(self, deal_id, fields):
        deal = self._crm_get_deal(deal_id)
        if "error" in deal:
            return deal
        if not isinstance(fields, dict) or "deal_id" in fields:
            return {"error": "fields must be an object without deal_id"}
        deal.update(fields)
        return {"ok": True, "deal": deal}

    def _crm_create_deal(self, fields):
        if not isinstance(fields, dict):
            return {"error": "fields must be an object"}
        deals = self.s.setdefault("deals", [])
        deal = {"deal_id": f"D-{900 + len([d for d in deals if d.get('created')]) + 1}", "account": "",
                "contact_email": "", "owner": "", "stage": "", "amount": "", "close_date": "", "next_step": "",
                **{k: v for k, v in fields.items() if k != "deal_id"}, "created": True}
        deals.append(deal)
        return {"ok": True, "deal": deal}

    # ---- calendar
    def _calendar_open_slots(self):
        return self.s.get("slots", [])
