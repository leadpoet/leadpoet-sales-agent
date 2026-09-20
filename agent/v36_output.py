"""Policy-selected wire output, separate from the internal evidence records.

Admission checks ground the internal signals in retrieved evidence. This module
uses only those admitted signal records for Intent Details; it cannot predict
the independent verifier's verdict and makes no qualification claims.
"""
from dataclasses import dataclass
from datetime import date as ISODate
from typing import Any
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from experiments.harness_bakeoff.models import _public_http_url, RequiredAttributeEvidence


def prose(value):
    return ' '.join(''.join(c if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs') else ' '
                           for c in str(value)).replace('```', '').split())


def validate_details(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError('Intent Details must contain 1–2000 characters')
    if re.search(r'\n[ \t\r]*\n|(?:^|\n)\s*(?:#{1,6}\s|[-*•]\s|\d+[.)]\s|>)|```', value):
        raise ValueError('Intent Details must be a prose paragraph')
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') and c not in '\r\n\t' for c in value):
        raise ValueError('Intent Details contains a control character')
    return ' '.join(value.split())


@dataclass(frozen=True)
class VerifiedActivity:
    """Locally grounded admission record, not an independent judge verdict."""
    quote: str
    source_url: str
    source_date: str
    criterion: int


def admitted_activities(signals):
    records = []
    for signal in signals:
        # Internal signals reach this boundary only after the admission checks.
        quote = prose(signal.get('snippet') or signal.get('description') or '')
        if not quote:
            continue
        records.append(VerifiedActivity(quote, signal['url'], str(signal.get('date') or ''),
                                        int(signal['matched_icp_signal'])))
    return tuple(records)


def intent_details(records: tuple[VerifiedActivity, ...]):
    """No company profile, fit prose, cache, or unadmitted signal is an input."""
    if not records:
        raise ValueError('An admitted signal record is required for Intent Details')
    closing = ' These reported activities connect to the ICP criteria identified by the corresponding submitted signals.'
    parts, seen = [], set()
    for record in records:
        quote = prose(record.quote)
        if not quote or quote.casefold() in seen:
            continue
        seen.add(quote.casefold())
        prefix = 'The source reports'
        if record.source_date:
            try:
                when = ISODate.fromisoformat(record.source_date).isoformat()
            except ValueError:
                when = ''
            if when:
                prefix = 'The source dated ' + when + ' reports'
        # Quoting preserves qualifiers and does not infer an event date from a
        # publication date. No commercial implication is invented.
        sentence = prefix + ': “' + quote + '”'
        if not quote.endswith(('.', '!', '?')):
            sentence += '.'
        if len(' '.join(parts + [sentence])) + len(closing) <= 2000:
            parts.append(sentence)
    if not parts:
        raise ValueError('Admitted source quote exceeds Intent Details capacity')
    return validate_details(' '.join(parts) + closing)


class SignalV5(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    matched_icp_signal: int = Field(ge=0)
    description: str = Field(min_length=1)
    url: str
    date: ISODate | None = None

    @field_validator('url')
    @classmethod
    def public_url(cls, value):
        return _public_http_url(value)


class CompanyV5(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    company_name: str = Field(min_length=1)
    company_website: str
    company_linkedin: Any = None
    industry: str
    employee_count: str
    company_stage: str = ''
    country: str
    state: Any = None
    intent_details: str = Field(min_length=1, max_length=2000)
    intent_signals: list[SignalV5] = Field(min_length=1)
    required_attribute: RequiredAttributeEvidence | None = None
    contact: Any = None

    @field_validator('company_website')
    @classmethod
    def public_url(cls, value):
        return _public_http_url(value)

    @field_validator('intent_details')
    @classmethod
    def details(cls, value):
        return validate_details(value)


def output_companies(companies, icp, trace=lambda *_: None):
    from agent.v27_policies import schema, contacts
    selected = schema(icp)
    if selected != 'leadpoet.lab_arena.output.v5':
        # Only v2 and v4 carry a contact outside v5; emitting one under v1 or v3 fails the
        # whole document at the boundary, which is how a round returned zero companies for
        # us on 2026-09-16. Strip it rather than trusting the policy markers to agree.
        stripped = 0
        if selected not in ('leadpoet.lab_arena.output.v2', 'leadpoet.lab_arena.output.v4'):
            trimmed = []
            for company in companies:
                if company.get('contact') is None:
                    trimmed.append(company)
                    continue
                stripped += 1
                trimmed.append({k: v for k, v in company.items() if k != 'contact'})
            companies = trimmed
        trace('output.schema_selected', {'schema': selected, 'has_intent_details': False,
              'has_contact': any(bool(c.get('contact')) for c in companies),
              'contacts_stripped': stripped})
        return companies
    converted = []
    company_keys = set(CompanyV5.model_fields) - {'intent_details', 'intent_signals', 'contact'}
    signal_keys = set(SignalV5.model_fields)
    for company in companies:
        row = {k: v for k, v in company.items() if k in company_keys}
        row['intent_details'] = intent_details(admitted_activities(company['intent_signals']))
        row['intent_signals'] = [{k: v for k, v in s.items() if k in signal_keys}
                                 for s in company['intent_signals']]
        if contacts(icp) and company.get('contact') is not None:
            row['contact'] = company['contact']
        converted.append(row)
    if len(converted) > 5:
        raise ValueError('At most five companies may be emitted')
    TypeAdapter(list[CompanyV5]).validate_python(converted)
    trace('output.schema_selected', {'schema': selected, 'has_intent_details': bool(converted),
          'has_contact': any(bool(c.get('contact')) for c in converted)})
    return converted
