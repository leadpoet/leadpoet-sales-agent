"""Whole-call price bounds from the live catalog description; never a guessed or observed price.

Reservations are estimates, not provider guarantees or bills. Actual returned
billing settles the existing ledger; a higher charge blocks further spending.
"""

import budget_guard as budget


QUANTITY_INPUTS = {"limit", "page_size", "pageSize", "num", "numResults", "pages", "maxPages", "max_pages",
                   "max_results", "maxResults", "perPage", "per_page", "count", "size"}


def _may_be_a_number(spec):
    kinds = (spec or {}).get("type")
    return any(kind in (None, "integer", "number") for kind in (kinds if isinstance(kinds, list) else [kinds]))


def inputs_can_bound(contract):
    """True when the catalog publishes a rate and the caller chooses how many units are billed.

    Such a call is never billed unbounded under actual cost: the model picks the inputs, so
    a quantity this module cannot price must not become a way around the cap. Only an input
    that may be a number counts, so a filter that happens to be called `size` does not. An
    untyped input, a union such as ["integer", "null"], or two declarations that disagree
    all count, so an unclear catalog entry is refused, not billed unbounded. Every
    other call stays billed as before because this module has no rule that can bound it:
    no catalog rate, or a published rate whose quantity is fixed by the provider or carried
    by an array input.
    """
    schema = contract.get("inputSchema", {})
    declared = [(f["name"], f) for f in schema.get("fields", [])]
    declared += list(((schema.get("jsonSchema") or {}).get("properties") or {}).items())
    chosen = any(name in QUANTITY_INPUTS and _may_be_a_number(spec) for name, spec in declared)
    return (contract.get("pricing") or {}).get("creditsPerUnit") is not None and chosen


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
    if bound is None:
        raise ValueError("No whole-call price is available for these options. Use a priced configuration or report the missing rate; do not guess max_cost_credits. No paid call was made.")
    if override is not None:
        supplied = budget.amount(override, "whole-call price reservation")
        if supplied < bound:
            raise ValueError("Supplied price bound is below the catalog-derived whole-call cost")
        return float(supplied)
    return float(bound)
