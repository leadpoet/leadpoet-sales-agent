"""Translate concise research inputs into the existing run/attempt/review records.

No dispatch, company qualification, strategy selection or automatic retries live
here. The research agent supplies those judgments; existing helpers enforce them.
"""

import copy
from datetime import datetime, timezone
import hashlib
import importlib
import os
from pathlib import Path
import re
import uuid

import budget_guard


def object_fields(value, allowed, label):
    hint = f"{label} accepts only: {', '.join(sorted(allowed))}"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object; {hint}")
    unexpected = set(value) - set(allowed)
    if unexpected:
        raise ValueError(f"{label} has unexpected fields: {', '.join(sorted(map(str, unexpected)))}; {hint}")


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def strings(value, label, *, empty=False):
    if not isinstance(value, list) or not empty and not value:
        raise ValueError(f"{label} must be a {'non-empty ' if not empty else ''}array")
    for item in value:
        text(item, label)


REQUEST_FIELDS = {"target_count", "icp", "buying_signals", "time_window", "budget", "signal_match_mode", "run_id",
                  "as_of_date", "max_duration_seconds", "product_service", "original_text", "signal_candidates_max",
                  "trait_signals", "seen_companies", "bound_required_attributes", "single_source_attributes", "workbook"}
# The most held companies a run may export for signal research downstream (leads.json signal_candidates).
SIGNAL_CANDIDATES_LIMIT = 50


def paraphrased_criteria(request, *, bound_exclusions=False):
    """Required criteria of a new run that are not the user's own words in original_text.

    Live runs rewrote the brief before any research: a buyer-hire signal gained a source restriction the brief never
    stated, "central purchasing" became "group-level or central", every run added an industry filter the brief never
    named, and a signal could keep its words but drop its named sources. Criteria copied from original_text keep its
    named sources, alternatives, dates and scope; interpretation belongs in research, not in the saved request.
    """
    import unicodedata
    from source_receipts import quote_form
    original = request.get("original_text")
    if not original:
        return []
    marks = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2212": "-", "\u02bc": "'", "*": ""})

    def fold(value):
        text = unicodedata.normalize("NFKC", str(value)).translate(marks).replace("\u2026", "...")
        return quote_form(text).casefold()

    # One folded line per source line, so a signal query must end where its sentence, bullet or line ends.
    brief, line_ends = "", set()
    for line in str(original).splitlines():
        if folded := fold(line):
            brief += (" " if brief else "") + folded
            line_ends.add(len(brief))

    def quoted(value, *, whole=False):
        needle = fold(value).strip(" .;:,!?")
        for found in re.finditer(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", brief) if needle else ():
            # French typography puts a space before ";", ":", "!" and "?".
            if not whole or found.end() in line_ends or brief[found.end():].lstrip(" ")[:1] in {".", ";", ":", "!", "?"}:
                return True
        return False

    icp = request["icp"]
    keys = ["company_types", "industries", "geographies", "required_attributes"] + ([] if bound_exclusions else ["exclusions"])
    found = [f"icp.{key}[{index}]" for key in keys for index, value in enumerate(icp.get(key, [])) if not quoted(value)]
    for index, signal in enumerate(request["buying_signals"]):
        if signal["importance"] != "required":
            continue
        query = signal.get("query") or signal["kind"]
        if not quoted(query, whole=True):
            found.append(f"buying_signals[{index}] ({signal['kind']})")
        # A source list may only restate the sources the signal's own words name.
        own = fold(query)
        if any(not re.search(r"(?<!\w)" + re.escape(fold(item).strip(" .;:,")) + r"(?!\w)", own)
               for item in signal.get("source_preferences", [])):
            found.append(f"buying_signals[{index}].source_preferences")
    return found


DOMAIN = re.compile(r"(?<![\w@.-])(?:www\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])", re.IGNORECASE)
# Legal-form and group words that may stand beside a company's name.
LEGAL_WORDS = {"inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation", "co", "company",
               "group", "groupe", "holding", "holdings", "the", "sa", "sas", "sasu", "sarl", "eurl", "gmbh", "ag", "kg",
               "se", "sl", "bv", "nv", "plc", "spa", "srl", "pty", "ab", "oy"}


def named_company(value):
    """The domain when a criterion is only one company's name and domain ("YipitData, yipitdata.com"), else None."""
    import unicodedata
    # NFD drops accents but keeps ™ and ℠ as marks; NFKD would spell them "TM" and "SM" into the name.
    text = "".join(c for c in unicodedata.normalize("NFD", str(value)) if not unicodedata.combining(c))
    text = re.sub(r"https?://", " ", text, flags=re.IGNORECASE)  # A link to the site names its domain too.
    domains = {match.group(1).casefold() for match in DOMAIN.finditer(text)}
    if len(domains) != 1:
        return None
    domain = domains.pop()
    words = re.findall(r"[^\W_]+", DOMAIN.sub(" ", text).casefold())
    names = {"".join(words), "".join(w for w in words if w not in LEGAL_WORDS)}
    return domain if words and re.sub(r"[^a-z0-9]", "", domain.split(".")[0]) in names else None


def normalize_request(value, run_file, *, saved=None, started_at=None):
    """Apply mechanical defaults to new inputs; never infer criteria or intent."""
    from validate_run import CONTACT_FIELDS, CONTACT_HANDOFF
    if isinstance(value, dict) and (contact := sorted(CONTACT_FIELDS & value.keys())):
        raise ValueError("TYCHE sources and qualifies companies; remove " + ", ".join(contact) + " from the request. "
                         + CONTACT_HANDOFF + " Nothing was saved.")
    object_fields(value, REQUEST_FIELDS, "request")
    request = copy.deepcopy(value)
    prior = saved or {}
    if "original_text" in request:
        text(request["original_text"], "original_text")
    if "product_service" in request:
        offering = request["product_service"]
        object_fields(offering, {"description", "perspective"}, "product_service")
        text(offering.get("description"), "product_service.description")
        if offering.get("perspective") not in {"seller", "target"}:
            raise ValueError("product_service.perspective must be seller or target")
    target = budget_guard.count(request.get("target_count"), "target_count")
    if not target:
        raise ValueError("target_count must be positive")
    # Bound at startup from request-options.json, never transcribed; a resume keeps the saved bound.
    if "signal_candidates_max" in prior:
        request.setdefault("signal_candidates_max", prior["signal_candidates_max"])
    if "signal_candidates_max" in request:
        bound = request["signal_candidates_max"]
        if isinstance(bound, bool) or not isinstance(bound, int) or not 0 <= bound <= SIGNAL_CANDIDATES_LIMIT:
            raise ValueError(f"signal_candidates_max must be a whole number from 0 to {SIGNAL_CANDIDATES_LIMIT}. Nothing was saved.")
        if "signal_candidates_max" in prior and prior["signal_candidates_max"] != bound:
            raise ValueError("signal_candidates_max differs from the saved run; preserve this run's settings. Nothing was saved.")
    # A host that takes results.json only (ARETE) binds workbook false in request-options.json; a resume keeps it.
    if "workbook" in prior:
        request.setdefault("workbook", prior["workbook"])
    if "workbook" in request:
        if request["workbook"] is not False:
            raise ValueError("workbook is set only to false, from request-options.json, by a host that takes no workbook. "
                             "Nothing was saved.")
        if "workbook" in prior and prior["workbook"] != request["workbook"]:
            raise ValueError("workbook differs from the saved run; preserve this run's settings. Nothing was saved.")
    if not isinstance(request.get("icp"), dict) or not request["icp"]:
        raise ValueError("icp must contain the user criteria")
    icp = request["icp"]
    if "bound_required_attributes" in request:
        strings(request["bound_required_attributes"], "bound_required_attributes")
        if any(value not in icp.get("required_attributes", []) for value in request["bound_required_attributes"]):
            raise ValueError("bound_required_attributes must remain in icp.required_attributes")
    object_fields(icp, {"company_types", "industries", "geographies", "company_size",
                       "required_attributes", "exclusions"}, "icp")
    for key, values in icp.items():
        if key == "company_size":
            object_fields(values, {"min_employees", "max_employees"}, "company_size")
            if not values:
                raise ValueError("company_size must specify a bound")
            for name, number in values.items():
                budget_guard.count(number, name)
            if values.get("min_employees", 0) > values.get("max_employees", float("inf")):
                raise ValueError("company_size bounds are reversed")
        else:
            strings(values, "icp." + key, empty=key == "exclusions")
    if not isinstance(request.get("buying_signals"), list) or not request["buying_signals"]:
        raise ValueError("buying_signals must contain the agent's interpreted signals")
    window = request.setdefault("time_window", {})
    object_fields(window, {"max_age_days", "max_age_months", "as_of_date"}, "time_window")
    from validate_run import _identity, signal_request_errors
    if prior and (errors := signal_request_errors(prior)):
        raise ValueError("Invalid saved request: " + "; ".join(errors))
    signal_keys = set()
    for signal in request["buying_signals"]:
        object_fields(signal, {"kind", "query", "min_age_days", "max_age_days", "max_age_months", "source_preferences", "importance"}, "signal")
        text(signal.get("kind"), "signal.kind")
        key = _identity(signal["kind"])
        if not key or key in signal_keys:
            raise ValueError("signal kinds must be distinct, non-empty identifiers within this request")
        signal_keys.add(key)
        # Resuming keeps the saved importance; a new signal without one is required.
        previous = [s for s in prior.get("buying_signals", []) if _identity(s.get("kind")) == key]
        signal.setdefault("importance", previous[0].get("importance", "required") if len(previous) == 1 else "required")
        if signal["importance"] not in {"required", "preferred"}:
            raise ValueError("signal.importance must be required or preferred")
        if "query" in signal:
            text(signal["query"], "signal.query")
        if "source_preferences" in signal:
            strings(signal["source_preferences"], "source_preferences", empty=True)
    # Bound at startup from request-options.json, never transcribed; a resume keeps the saved list. A trait signal (what
    # the company is, uses or does) may be exported as likely; every other kind is an event and needs a confirming source.
    if "trait_signals" in prior:
        request.setdefault("trait_signals", prior["trait_signals"])
    if "trait_signals" in request:
        traits = request["trait_signals"]
        if (not isinstance(traits, list) or any(not isinstance(k, str) or not _identity(k) for k in traits)
                or len({_identity(k) for k in traits}) != len(traits)):
            raise ValueError("trait_signals must be a list of distinct signal kinds. Nothing was saved.")
        if traits and not request.get("signal_candidates_max"):
            raise ValueError("trait_signals needs signal_candidates_max above 0, since a likely trait is exported only as "
                             "a signal candidate. Nothing was saved.")
        if missing := [kind for kind in traits if _identity(kind) not in signal_keys]:
            raise ValueError(f"trait_signals names {missing}, which no buying_signals kind matches: transcribe each "
                             "signal's kind exactly as request.txt names it. Nothing was saved.")
        if "trait_signals" in prior and prior["trait_signals"] != traits:
            raise ValueError("trait_signals differs from the saved run; preserve this run's settings. Nothing was saved.")
    # A continuation's companies already seen in earlier runs for this request: bound from request-options.json as written,
    # kept on resume. An entry that is not a company key (a name) is kept and matches only the same name used as a target.
    if "seen_companies" in prior:
        request.setdefault("seen_companies", prior["seen_companies"])
    if "seen_companies" in request:
        if not isinstance(request["seen_companies"], list) or any(not isinstance(v, str) for v in request["seen_companies"]):
            raise ValueError("seen_companies must be a list of company domains or LinkedIn company URLs. Nothing was saved.")
        if "seen_companies" in prior and prior["seen_companies"] != request["seen_companies"]:
            raise ValueError("seen_companies differs from the saved run; preserve this run's settings. Nothing was saved.")
    # The attribute texts a host publishes as one quote, bound from request-options.json; kept on resume.
    if "single_source_attributes" in prior:
        request.setdefault("single_source_attributes", prior["single_source_attributes"])
    if "single_source_attributes" in request:
        named = request["single_source_attributes"]
        if not isinstance(named, list) or not named or not all(isinstance(t, str) and t.strip() for t in named):
            raise ValueError("single_source_attributes is the list of required attribute texts request-options.json "
                             "binds; omit it otherwise. Nothing was saved.")
        if prior and prior.get("single_source_attributes") != named:
            raise ValueError("single_source_attributes differs from the saved run; preserve this run's settings. "
                             "Nothing was saved.")
    if errors := signal_request_errors(request):
        raise ValueError("; ".join(errors))
    # Startup binds a request-exclusions.json list beside the run as written, without model transcription.
    sidecar = Path(run_file).with_name("request-exclusions.json")
    bound = sidecar.exists() or sidecar.is_symlink()
    if not prior and (paraphrased := paraphrased_criteria(request, bound_exclusions=bound)):
        raise ValueError(", ".join(paraphrased) + ": not in original_text. Copy each company type, industry, geography, "
                         "required attribute, exclusion and required signal's query verbatim from original_text as one "
                         "contiguous passage in the user's own words (\"French\", not \"France\", if that is what it says), "
                         "keeping its named sources, alternatives, dates and scope; a signal's query runs to the end of its "
                         "sentence, bullet or line, and source_preferences only repeat sources that query names. Do not "
                         "paraphrase, add or drop words, and omit a filter the user did not state. Nothing was saved.")
    # A company has one own domain, so separate required attributes that each only name a different company can
    # never all hold: a live run saved three named accounts this way and rejected all three.
    named = {f"icp.required_attributes[{i}]": d for i, v in enumerate(icp.get("required_attributes", []))
             if (d := named_company(v))}
    if not prior and len(set(named.values())) > 1:
        raise ValueError(", ".join(named) + " each only name a different company (" + ", ".join(sorted(set(named.values())))
                         + "), and one company has one domain, so none can meet them all. If the brief names them as "
                         "the only companies to qualify, save each as a company type, copied verbatim (any one is "
                         "enough). If the brief relates the company to them (uses, sells, belongs to, works with), save "
                         "the brief's words for that relation together with the list as one criterion. Nothing was saved.")
    date = (started_at or datetime.now(timezone.utc).isoformat())[:10]
    fallback_id = Path(run_file).parent.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", fallback_id):
        fallback_id = "run-" + hashlib.sha256(str(Path(run_file).resolve()).encode()).hexdigest()[:16]
    defaults = {"signal_match_mode": "any", "run_id": fallback_id,
                "as_of_date": window.get("as_of_date", date)}
    if not prior:
        defaults["max_duration_seconds"] = None
    elif "max_duration_seconds" in prior:
        defaults["max_duration_seconds"] = prior["max_duration_seconds"]
    for key, default in defaults.items():
        request.setdefault(key, copy.deepcopy(prior.get(key, default)))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", text(request["run_id"], "run_id")):
        raise ValueError("run_id must be a safe identifier")
    for value in [request["as_of_date"], *([window["as_of_date"]] if "as_of_date" in window else [])]:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("as_of_date must be YYYY-MM-DD")
        datetime.strptime(value, "%Y-%m-%d")
    if request["signal_match_mode"] not in {"any", "all"}:
        raise ValueError("signal_match_mode must be any or all")
    if request.get("max_duration_seconds") is not None and not budget_guard.count(request["max_duration_seconds"], "max_duration_seconds"):
        raise ValueError("max_duration_seconds must be positive")
    from validate_run import run_deadline
    run_deadline({"request": request, "stop_check": {"started_at": started_at or datetime.now(timezone.utc).isoformat()}})
    return request


def closing_window(requested, duration):
    """Seconds research closes before an explicit time limit; a short run keeps nine tenths of it."""
    if not requested or not duration:
        return 0
    return min(budget_guard.count(requested, "closing_seconds"), duration // 10)


def start_document(run_file, setup, *, existing=None, ledger=None):
    """Produce validated initialization inputs; budget_guard owns persistence."""
    object_fields(setup, {"request", "max_usd", "scrapingdog_usd_per_credit", "started_at", "budget_policy",
                          "closing_seconds", "unpriced_calls"}, "setup")
    existing, ledger = existing or {}, ledger or {}
    from validate_run import schema_error
    if existing and (error := schema_error(existing)):
        raise ValueError(error)
    started = existing.get("stop_check", {}).get("started_at") or ledger.get("initial_started_at") or setup.get("started_at") or datetime.now(timezone.utc).isoformat()
    text(started, "started_at")
    parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("started_at must include a timezone")
    if setup.get("started_at", started) != started:
        raise ValueError("resume must preserve the original started_at")
    request = normalize_request(setup.get("request"), run_file,
                                saved=existing.get("request"), started_at=started)
    if existing and setup.get("request") == existing.get("request"):
        request = copy.deepcopy(existing["request"])
    cap = setup.get("max_usd", ledger.get("usd_limit", float(request["target_count"] * budget_guard.DEFAULT_USD_PER_COMPANY)))
    options = dict(max_usd=cap,
        scrapingdog_usd_per_credit=setup.get("scrapingdog_usd_per_credit", ledger.get("usd_per_credit", {}).get("scrapingdog")))
    budget_defaults = existing.get("request", {}).get("budget", {
        "deepline_credits": float(budget_guard.amount(cap, "max_usd") / budget_guard.amount("0.10", "rate")),
        "scrapingdog_credits": 0, "hard_stop": True})
    request.setdefault("budget", copy.deepcopy(budget_defaults))
    budget = request["budget"]
    object_fields(budget, {"deepline_credits", "scrapingdog_credits", "hard_stop", "max_paid_calls",
                           "max_deepline_credits_per_next_lead"}, "request.budget")
    if budget.get("hard_stop") is not True:
        raise ValueError("request.budget.hard_stop must be true")
    # Default each omitted allowance independently. Disabling one provider must
    # not disable another; explicit caps (including zero) and saved limits win.
    budget = request["budget"] = {**copy.deepcopy(budget_defaults), **budget}
    for key, value in budget.items():
        if key == "max_paid_calls":
            budget_guard.count(value, key)
        elif key != "hard_stop" and type(value) not in (int, float):
            raise ValueError(f"request.budget.{key} must be a number")
    for provider in budget_guard.PROVIDERS:
        if not existing:
            budget.setdefault(provider + "_credits", 0)
        budget_guard.amount(budget.get(provider + "_credits", 0), provider)
    if existing and request != existing["request"]:
        raise ValueError("request differs from saved run; resume the authoritative criteria")
    limits = {provider + "_credits": 0 for provider in budget_guard.PROVIDERS}
    limits.update({k: v for k, v in budget.items() if k != "hard_stop"})
    saved_policy = existing.get("budget", {}).get("policy", "reserved" if ledger.get("version") == 1 else "actual_cost")
    policy = setup.get("budget_policy", saved_policy)
    if policy not in {"actual_cost", "reserved"} or (existing or ledger) and policy != saved_policy:
        raise ValueError("Budget policy must be supported and preserve the saved ledger")
    # Saved once with the ledger: a resumed run keeps the contract it started with. A ledger
    # that has to be rebuilt has nothing saved, so it takes what this host asks for.
    options["unpriced_calls"] = (ledger.get("unpriced_calls") if ledger
                                 else setup.get("unpriced_calls") or os.environ.get("TYCHE_UNPRICED_CALLS"))
    from validate_run import RESULT_SCHEMA_VERSION, criteria
    document = dict(schema_version=RESULT_SCHEMA_VERSION, run_id=request.get("run_id", existing.get("run_id")), retrieved_at=started,
        request=request, criteria=criteria(request), budget={"policy": policy, "limits": limits,
                                "spent": {"deepline_credits": 0, "scrapingdog_credits": 0}, "paid_calls": 0, "status": "within_budget"},
        routes=[], accepted=[], rejected=[], unresolved=[], summary={},
        stop_check={"started_at": started, "next_actions": []}, stop_audit={"route_frontier": []})
    closing = closing_window(setup.get("closing_seconds"), request.get("max_duration_seconds"))
    if not existing and closing:
        document["stop_check"]["closing_seconds"] = closing  # Saved once with the run.
    return document, options


def normalize_provider_request(provider, request, label):
    """Use the existing adapter contract for both research and legacy inputs."""
    if provider not in ("deepline", "scrapingdog", "public_web"):
        raise ValueError(f"{label}.provider must use an existing provider wrapper: deepline, scrapingdog, public_web")
    if not isinstance(request, dict):
        raise ValueError(f"{label}.request must contain the provider operation and inputs")
    request = copy.deepcopy(request)
    adapter = None if provider == "public_web" else importlib.import_module(provider)
    if adapter:
        try:
            request = adapter._validate_request(request) if provider == "deepline" else adapter.validate_request(request)
        except ValueError as exc:
            raise ValueError(f"{label}.request: {exc}") from exc
    return adapter, request


def prepare_lookup(value, label="lookup"):
    """The agent selects target, purpose and request; derive bookkeeping only."""
    object_fields(value, {"provider", "request", "scope", "phase", "purpose", "approach",
                          "max_cost_credits", "status_read"}, label)
    provider = value.get("provider", "deepline")
    _, request = normalize_provider_request(provider, value.get("request"), label)
    catalog = provider == "deepline" and request.get("operation") in {"search", "describe"}
    paid = provider == "scrapingdog" or provider == "deepline" and request.get("operation") == "execute"
    purpose = value.get("purpose", f"Inspect {request.get('tool') or request.get('query', '')}" if catalog else None)
    phase = "account_discovery" if catalog else value.get("phase")
    from validate_run import company_domain
    scope = "discovery" if catalog else company_domain(text(value.get("scope"), label + ".scope"))
    action = dict(id="lookup-" + uuid.uuid4().hex[:24], scope=scope, phase=phase,
        description=text(purpose, label + ".purpose"), approach=value.get("approach", "capability-discovery" if catalog else purpose),
        provider=provider, paid_calls=int(paid), cost_upper_bound_credits=value.get("max_cost_credits") if paid else 0)
    if "status_read" in value:
        action["status_read"] = value["status_read"]
    return {"action": action, "request": request}


def check_tool_contract(receipt, request):
    """Validate against the saved live contract before reservation or dispatch."""
    matches = [row for row in receipt.get("results", []) if isinstance(row, dict)
               and request["tool"] in {row.get("toolId"), row.get("id"), row.get("tool")}]
    if len(matches) != 1:
        raise ValueError("saved description does not identify this tool; refresh its description")
    contract = matches[0]
    if contract.get("disabled") or contract.get("connected") is False or contract.get("callable") is False:
        raise ValueError("saved tool description reports this operation unavailable")
    schema = contract.get("inputSchema")
    if not isinstance(schema, dict):
        raise ValueError("saved description has no input schema; refresh its description")
    native = schema.get("jsonSchema", {})
    fields = schema.get("fields", [])
    if (not isinstance(native, dict) or not isinstance(fields, list)
            or any(not isinstance(f, dict) or not isinstance(f.get("name"), str) for f in fields)):
        raise ValueError("saved description has malformed input fields; refresh its description")
    payload = request["payload"]
    required = {f["name"] for f in fields if f.get("required")}
    missing = required - set(payload)
    if missing:
        raise ValueError("provider payload missing required fields: " + ", ".join(sorted(missing)))
    for field in fields:
        name, kind = field.get("name"), field.get("type")
        if name not in payload:
            continue
        value = payload[name]
        valid = {"string": isinstance(value, str), "boolean": type(value) is bool,
                 "integer": type(value) is int, "number": type(value) in (int, float),
                 "object": isinstance(value, dict), "array": isinstance(value, list)}
        if kind in valid and not valid[kind]:
            raise ValueError(f"provider payload.{name} must be {kind}")
    _check_native_schema(native, payload)


def _catalog_schema(schema):
    """Translate Deepline's observed `type: any` shorthand at schema nodes.

    The raw descriptor stays intact. Literal data in enum/default/const is not
    a schema; all other constraints and malformed contracts still validate.
    """
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    if result.get("type") == "any":
        del result["type"]
    for key, value in result.copy().items():
        if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"} and isinstance(value, dict):
            result[key] = {name: _catalog_schema(child) for name, child in value.items()}
        elif key in {"allOf", "anyOf", "oneOf", "prefixItems", "items"} and isinstance(value, list):
            result[key] = [_catalog_schema(child) for child in value]
        elif key in {"additionalProperties", "unevaluatedProperties", "propertyNames", "items", "additionalItems",
                     "unevaluatedItems", "contains", "not", "if", "then", "else", "contentSchema"}:
            result[key] = _catalog_schema(value)
    return result


def _check_native_schema(schema, payload):
    if not schema:  # Some catalog tools expose only the field list above.
        return
    try:
        from jsonschema.exceptions import SchemaError, best_match
        from jsonschema.validators import validator_for
        from referencing import Registry
        from referencing.exceptions import Unresolvable
    except ImportError as exc:
        raise ValueError("Provider input validation requires the Python dependencies; "
                         "install requirements.txt with the launcher's Python interpreter") from exc
    schema = _catalog_schema(schema)
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise ValueError("saved input schema is malformed; refresh its description")
    validator = validator_for(schema, default=None) if "$schema" in schema else validator_for(schema)
    if validator is None:
        raise ValueError("saved input schema uses an unsupported JSON Schema version; refresh its description")
    try:
        validator.check_schema(schema)
        # Resolve embedded references, but never fetch external schemas or URLs.
        error = best_match(validator(schema, registry=Registry()).iter_errors(payload))
    except SchemaError as exc:
        raise ValueError("saved input schema is malformed; refresh its description") from exc
    except Unresolvable as exc:
        raise ValueError("saved input schema has an unresolved reference; refresh its description "
                         "with a self-contained schema") from exc
    if error is not None:
        path = "payload" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}"
                                 for part in error.absolute_path)
        allowed = (f"; allowed fields: {sorted(error.schema['properties'])}"
                   if error.validator == "additionalProperties" and isinstance(error.schema.get("properties"), dict) else "")
        raise ValueError(f"provider {path}: {error.message[:1200]}{allowed[:1200]}. Correct the input using the saved schema")


def company_update(document, item):
    """Apply explicit field and criterion updates, preserving unrelated evidence."""
    object_fields(item, {"scope", "state", "stage", "reason_text", "company",
                         "qualification_checks", "supporting_findings", "account_fit"}, "company update")
    from validate_run import _company_key, _identity, company_domain, company_website, criteria, excluded_company
    scope = company_domain(text(item.get("scope"), "company update.scope"))
    matches = [(state, row) for state in ("accepted", "unresolved", "rejected") for row in document.get(state, [])
               if _company_key(row) == scope and (state == "accepted" or row.get("stage") == "account")]
    if len(matches) > 1:
        raise ValueError("company has multiple saved records; reconcile before updating")
    old_state, old = matches[0] if matches else ("unresolved", {})
    row = copy.deepcopy(old)
    state = item.get("state", old_state)
    if state not in {"accepted", "unresolved", "rejected"}:
        raise ValueError("company state must be accepted, unresolved or rejected")
    if item.get("stage", "account") != "account":
        raise ValueError("A held or rejected company has stage account; people belong to the contact stage (DEMOS)")
    company = copy.deepcopy(row.pop("company", row.pop("candidate", {"domain": scope})))
    if "company" in item:
        if not isinstance(item["company"], dict):
            raise ValueError("company facts must be an object")
        company.update(copy.deepcopy(item["company"]))
        for key in [key for key, value in company.items() if value is None]:
            del company[key]  # null clears a saved field, such as a range the newly selected record lacks.
        company["website"] = company_website(company)
    if state == "accepted" and "company" in company:
        name = company.pop("company")  # A candidate's discovered name becomes its canonical name.
        if isinstance(name, str) and not company.get("canonical_name"):
            company["canonical_name"] = name
    if _company_key({"company": company}) != scope:
        raise ValueError("company update cannot change its canonical identity")
    if company.get("domain"):
        company["domain"] = scope  # The stable join key: lower case, without www.
    row["company" if state == "accepted" else "candidate"] = company
    checks = row.setdefault("qualification_checks", [])
    if "qualification_checks" in item:
        if not isinstance(item["qualification_checks"], list):
            raise ValueError("qualification_checks must be an array")
        requested = {c["id"]: c for c in criteria(document["request"])}
        seen = set()
        for check in item["qualification_checks"]:
            object_fields(check, {"criterion_id", "status", "claim", "evidence", "support", "support_reason"},
                          "qualification check")
            criterion = requested.get(check.get("criterion_id"))
            if criterion is None:
                raise ValueError(f"Unknown criterion_id {check.get('criterion_id')!r}; select one of {sorted(requested)} "
                                 "from inspect(field='requirements'). Facts that answer no requested criterion belong "
                                 "in supporting_findings.")
            if criterion["id"] in seen:
                raise ValueError(f"duplicate criterion update: {criterion['id']}")
            seen.add(criterion["id"])
            if check.get("status") not in {"pass", "fail", "unknown"}:
                raise ValueError("each criterion needs status pass, fail or unknown")
            text(check.get("claim"), "claim")
            if not isinstance(check.get("evidence"), list) or check["status"] != "unknown" and not check["evidence"]:
                raise ValueError("pass/fail requires evidence; unknown may have an empty evidence array")
            # Code owns the label, importance and signal kind, so they always match the saved request.
            update = {"criterion_id": criterion["id"], "criterion": criterion["label"], "importance": criterion["importance"],
                      **({"signal": criterion["label"]} if criterion["kind"] == "signal" else {}),
                      "status": check["status"], "claim": check["claim"], "evidence": copy.deepcopy(check["evidence"]),
                      **{key: check[key] for key in ("support", "support_reason") if key in check}}
            position = next((index for index, saved in enumerate(checks) if saved.get("criterion_id") == criterion["id"]), None)
            if position is None:
                checks.append(update)
            else:
                checks[position] = update
    if "supporting_findings" in item:
        from validate_run import supporting_finding_errors
        errors = supporting_finding_errors(item["supporting_findings"], "supporting_findings")
        if errors:
            raise ValueError("; ".join(errors))
    for key in ("account_fit", "supporting_findings"):
        if key in item:
            row[key] = copy.deepcopy(item[key])  # Explicit complete replacements, never a recursive merge.
    # The initial signal is derived, never authored: the first passing check of a required signal kind.
    required = {_identity(s["kind"]) for s in document["request"].get("buying_signals", [])
                if s.get("importance", "required") == "required"}
    passing = [c for c in checks if c.get("signal") and c["status"] == "pass" and c["evidence"]
               and (not required or _identity(c["signal"]) in required)]
    row.pop("signal_evidence", None)
    if passing:
        evidence = passing[0]["evidence"][0]
        row["signal_evidence"] = {**{
            "evidence_" + key if key in {"url", "date", "date_basis", "text"} else key: value
            for key, value in evidence.items()},
            "criterion_id": passing[0]["criterion_id"], "criterion": passing[0]["criterion"], "signal": passing[0]["signal"]}
    if state != "unresolved":  # A likely trait is a held company's backup for signal research, never a lead's claim.
        for check in checks:  # Only a still-unknown check drops it; support on a pass or fail is refused below.
            if check.get("status") == "unknown":
                check.pop("support", None)
                check.pop("support_reason", None)
    if state == "accepted":
        for key in ("stage", "reason_code", "reason_text"):
            row.pop(key, None)
    else:
        row["stage"] = "account"
        if state == "rejected":
            excluded = excluded_company(document["request"], {"candidate": company}) or any(
                c["status"] == "fail" and c["criterion_id"].startswith("exclusion:") for c in checks)
            row["reason_code"] = "explicit_exclusion" if excluded else "not_icp_fit"
        else:
            row["reason_code"] = "missing_account_evidence"
        if "reason_text" in item:
            row["reason_text"] = item["reason_text"]
        text(row.get("reason_text"), "company update.reason_text")
    return {"state": state, "row": row}
