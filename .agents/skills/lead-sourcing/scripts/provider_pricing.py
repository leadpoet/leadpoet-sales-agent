"""Whole-call price bounds from the live catalog description; never a guessed or observed price.

Reservations are estimates, not provider guarantees or bills. Actual returned
billing settles the existing ledger; a higher charge blocks further spending.
"""

from decimal import ROUND_CEILING, Decimal

import budget_guard as budget


# Deepline publishes exa_search's rate only as text (live catalog, checked 2026-10-19). Only this exact text
# is priced, so a changed tariff is refused rather than guessed. Options it does not price are refused too.
EXA_SEARCH_TARIFF = (
    "Per request including 10 returned results: auto/fast/neural 0.098 Deepline credits; deep 0.168 Deepline "
    "credits; instant 0.056 Deepline credits. 0.014 Deepline credits per additional returned result. 0.014 Deepline "
    "credits per returned summary; search text and highlights are included. Combine these charges, then round the "
    "total up to 0.01 Deepline credits.")
_EXA_SEARCH_BASE = {"auto": "0.098", "fast": "0.098", "neural": "0.098", "deep": "0.168", "instant": "0.056"}
_EXA_SEARCH_INPUTS = {"query", "type", "category", "numResults", "includeDomains", "excludeDomains", "startCrawlDate",
                      "endCrawlDate", "startPublishedDate", "endPublishedDate", "includeText", "excludeText",
                      "userLocation", "moderation", "contents"}


def _exa_search_tariff(contract):
    pricing = contract.get("pricing") or {}
    return (contract.get("toolId", contract.get("id")) == "exa_search" and pricing.get("creditsPerUnit") is None
            and pricing.get("summary") == EXA_SEARCH_TARIFF)


def _exa_search_credits(inputs):
    """exa_search's whole-call bound from its published tariff, or None for options the tariff does not price."""
    contents = inputs.get("contents", {})
    kind, count = inputs.get("type", "auto"), inputs.get("numResults", 10)
    if (set(inputs) - _EXA_SEARCH_INPUTS or not isinstance(contents, dict) or set(contents) - {"text", "highlights", "summary"}
            or kind not in _EXA_SEARCH_BASE or type(count) is not int or not 1 <= count <= 100):
        return None
    summaries = count if "summary" in contents else 0  # The schema takes an options object; {} still bills summaries.
    total = Decimal(_EXA_SEARCH_BASE[kind]) + Decimal("0.014") * (max(0, count - 10) + summaries)
    return total.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


QUANTITY_INPUTS ={"limit", "page_size", "pageSize", "num", "numResults", "pages", "maxPages", "max_pages",
                   "max_results", "maxResults", "perPage", "per_page", "count", "size"}


def _may_be_a_number(spec):
    kinds = (spec or {}).get("type")
    return any(kind in (None, "integer", "number") for kind in (kinds if isinstance(kinds, list) else [kinds]))


def chooses_quantity(contract):
    """True when the caller chooses how many units are billed, through a quantity input that may be a number.

    Only an input that may be a number counts, so a filter that happens to be called `size` does not. An
    untyped input, a union such as ["integer", "null"], or two declarations that disagree all count, so an
    unclear catalog entry is refused rather than billed unbounded.
    """
    schema = contract.get("inputSchema", {})
    declared = [(f["name"], f) for f in schema.get("fields", [])]
    declared += list(((schema.get("jsonSchema") or {}).get("properties") or {}).items())
    return any(name in QUANTITY_INPUTS and _may_be_a_number(spec) for name, spec in declared)


def inputs_can_bound(contract):
    """True when the catalog publishes a rate and the caller chooses how many units are billed.

    Such a call is never billed unbounded under actual cost: the model picks the inputs, so
    a quantity this module cannot price must not become a way around the cap. Every
    other call stays billed as before because this module has no rule that can bound it:
    no catalog rate, or a published rate whose quantity is fixed by the provider or carried
    by an array input.
    """
    pricing = contract.get("pricing") or {}
    return chooses_quantity(contract) and (pricing.get("creditsPerUnit") is not None or _exa_search_tariff(contract))


def unpriced(contract):
    """True when Deepline bills this tool from usage after the call and publishes no rate: it has no whole-call price.

    Under unpriced_calls: hold such a call can be admitted at a provisional hold (budget_guard.unpriced_hold).
    """
    pricing = contract.get("pricing")
    return (isinstance(pricing, dict) and pricing.get("unit") == "usage" and pricing.get("creditsPerUnit") is None
            and not _exa_search_tariff(contract))


def call_credits(contract, inputs, override=None):
    pricing = contract.get("pricing") or {}
    rate, unit = pricing.get("creditsPerUnit"), pricing.get("unit")
    fields = {f["name"]: f for f in contract.get("inputSchema", {}).get("fields", [])}
    quantity = None
    if unit in ("call", "request"):
        quantity = 1
    elif unit == "page" and "page" in fields and not any(k in fields for k in ("pages", "maxPages", "max_pages")):
        quantity = 1
    elif unit == "result":
        if "limit" in fields:
            quantity = inputs.get("limit", fields["limit"].get("default"))
        elif "page_size" in fields:
            quantity = inputs.get("page_size", fields["page_size"].get("default"))
        elif contract.get("toolId", contract.get("id")) == "serper_google_search" and "num" in fields:
            # This endpoint's catalog defines num as the exact result count.
            quantity = inputs.get("num", fields["num"].get("default"))
    bound = None
    # A published but unsupported pricing unit is not replaced by a guess.
    if rate is not None and type(quantity) is int and quantity > 0:
        bound = budget.amount(rate, "catalog price") * quantity
    elif _exa_search_tariff(contract):
        bound = _exa_search_credits(inputs)
    if bound is None:
        raise ValueError("No whole-call price is available for these options. Use a priced configuration or report the missing rate; do not guess max_cost_credits. No paid call was made.")
    if override is not None:
        supplied = budget.amount(override, "whole-call price reservation")
        if supplied < bound:
            raise ValueError("Supplied price bound is below the catalog-derived whole-call cost")
        return float(supplied)
    return float(bound)
