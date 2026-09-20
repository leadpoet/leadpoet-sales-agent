"""Transcription of public scoring rules for self-checking fetched evidence (MIT).

Copyright (c) 2025 Leadpoet. See LICENSE.judge-mirror.
Unknown observations are not contradictions. This is not the LLM judge and
does not promise a score. All inputs are already-fetched evidence; no I/O.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timezone
import logging
import os
import re
from types import SimpleNamespace
from typing import Any, Final, Mapping, NamedTuple, Optional, Sequence, Tuple
from agent.safe_urls import urlsplit

logger = logging.getLogger(__name__)


def submitted_conflicts(company, icp):
    """Mirror the adapter's bucket admission and the fit merger's contradictions.

    Blank stage/country is unavailable, not a mismatch. Do not resurrect the
    retired binary-fit gate, which rejected missing stage without observation.
    """
    allowed = employee_count_buckets_for_icp(icp)
    observed = company.get('employee_count')
    bucket = normalize_employee_count_bucket(observed, default=None) or normalize_observed_employee_count_bucket(observed, default=None)
    if not bucket or bucket not in allowed:
        return ['adapter.employee_size']
    row = SimpleNamespace(employee_count=bucket, company_stage=company.get('company_stage', ''), country=company.get('country', ''))
    wanted_stage = icp.get('company_stage') or 'Any'
    if isinstance(wanted_stage, Sequence) and not isinstance(wanted_stage, (str, bytes, bytearray)):
        wanted_stage = next((str(v).strip() for v in wanted_stage if str(v).strip()), 'Any')
    wanted = SimpleNamespace(employee_count='|'.join(allowed), company_stage=str(wanted_stage).strip() or 'Any',
                             country=icp.get('country') or icp.get('geography') or 'United States',
                             geography=icp.get('geography') or icp.get('country') or 'United States')
    return ['fit.'+label for label, decision in (
        ('employee_size', _submitted_employee_size_decision(row, wanted)),
        ('stage', _submitted_stage_decision(row, wanted)),
        ('country', _submitted_geography_decision(row, wanted)),
    ) if decision == COMPANY_FIT_MISMATCH]


def signal_rejections(signal, page_content, claim, buyer_cap_days):
    """Only known deterministic rejections; absent page text remains unknown."""
    reasons = []
    if reason := check_url_structural_validity(signal.get('url') or ''):
        reasons.append(('intent.url', reason))
    if reason := check_evidence_freshness(claim, signal.get('date'), buyer_cap_days=buyer_cap_days):
        reasons.append(('intent.freshness', reason))
    text = page_content or ''
    if reason := check_antibot_wall(text):
        reasons.append(('intent.antibot', reason))
    snippet = signal.get('snippet') or ''
    # Match the helper's minimum lengths and 30% 4-gram boundary, not a
    # stricter exact-substring policy. No fetch means no content verdict.
    if len(snippet.strip()) >= 30 and len(text.strip()) >= 200:
        overlap = compute_snippet_overlap(snippet, text)
        if overlap < 0.30:
            reasons.append(('intent.snippet_overlap', f'{overlap:.0%}'))
    if snippet and len(text.strip()) >= 200:
        grounded, total, missing = check_snippet_signal_grounding(snippet, text)
        if total > 0 and grounded == 0:
            reasons.append(('intent.snippet_grounding', ', '.join(missing)))
    return reasons


def filter_company(company, icp, *, pages=None, identity=None, evaluation_date=None, log=None, admission=False):
    """Return a clean copy, or None for a deterministic company rejection.

    A failed secondary signal cannot invalidate a surviving primary signal.
    Unknown identity/page observations are logged, never invented as proof.
    This is a bounded precheck mirror, not a transcription of LLM scoring.
    """
    def emit(rule, **details):
        if log:
            log('judge_mirror', {'company': company.get('company_name'), 'rule': rule, **details})
    for rule in submitted_conflicts(company, icp):
        emit(rule, decision='reject')
        return None
    def identity_passes():
        if identity is not None:
            receipt = evaluate_company_identity(
                submitted_name=company.get('company_name'), submitted_website=company.get('company_website'),
                submitted_linkedin=company.get('company_linkedin'), observed_name=identity.get('name'),
                observed_website=identity.get('website'), observed_linkedin=identity.get('linkedin'),
                evidence_source=identity.get('source'))
            emit('identity.'+receipt['reason_code'], decision=receipt['decision'])
            return receipt['decision'] != COMPANY_FIT_MISMATCH
        emit('identity.not_observed', decision='unavailable')
        return True
    if not admission and not identity_passes():
        return None
    claims = icp.get('intent_signals') or [icp.get('intent_signal')]
    if isinstance(claims, (str, Mapping)):
        claims = [claims]
    cap = max(1, int(icp.get('intent_max_age_days') or 365))
    pages = pages or {}
    kept = []
    with use_evaluation_date(evaluation_date):
        for signal in company.get('intent_signals') or []:
            index = signal.get('matched_icp_signal')
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(claims):
                emit('intent.index', decision='reject_signal')
                continue
            claim = claims[index]
            if isinstance(claim, Mapping):
                claim = claim.get('intent_signal') or claim.get('signal') or claim.get('text') or ''
            page = pages.get(signal.get('url'))
            text = page.get('text', '') if isinstance(page, Mapping) else (page or '')
            if not text:
                emit('intent.page_not_observed', url=signal.get('url'), decision='unavailable')
            reasons = signal_rejections(signal, text, str(claim or ''), cap)
            if admission and index == 0:
                # Slot admission requires the agent's primary evidence to
                # exist, beyond the platform helper's unknown-state precheck.
                # This is not a claim to reproduce the remote LLM verdict.
                if len(text.strip()) < 200 or not signal.get('snippet'):
                    reasons.append(('admission.primary_not_observed', 'No fetched primary proof'))
            if reasons:
                for rule, detail in reasons:
                    emit(rule, detail=detail, url=signal.get('url'), decision='reject_signal')
            else:
                kept.append(dict(signal))
    if not any(s['matched_icp_signal'] == 0 for s in kept):
        emit('intent.no_primary_survives', decision='reject')
        return None
    if admission and not identity_passes():
        return None
    if admission:
        emit('admission.accept', decision='accept')
    return {**company, 'intent_signals': kept}

_MIRROR_DATE = ContextVar('mirror_evaluation_date', default=None)

def evaluation_datetime():
    value = _MIRROR_DATE.get()
    if value is None:
        raw = os.environ.get('LAB_ARENA_EVALUATION_DATE') or os.environ.get('BAKEOFF_EVALUATION_DATE')
        value = date.fromisoformat(raw) if raw else datetime.now(timezone.utc).date()
    return datetime.combine(value, time.min, tzinfo=timezone.utc)

@contextmanager
def use_evaluation_date(value):
    token = _MIRROR_DATE.set(date.fromisoformat(value) if isinstance(value, str) else value)
    try:
        yield
    finally:
        _MIRROR_DATE.reset(token)


# Deterministic helpers from qualification/employee_buckets.py

LINKEDIN_EMPLOYEE_BUCKETS = ('0-1', '2-10', '11-50', '51-200', '201-500', '501-1,000', '1,001-5,000', '5,001-10,000', '10,001+')

DEFAULT_EMPLOYEE_BUCKET = '51-200'

LEGACY_EMPLOYEE_BUCKET_MAP = {'1-10': '2-10', '10-50': '11-50', '50-200': '51-200', '200-500': '201-500', '500-1000': '501-1,000', '501-1000': '501-1,000', '1000-5000': '1,001-5,000', '1001-5000': '1,001-5,000', '5000-10000': '5,001-10,000', '5001-10000': '5,001-10,000', '5000+': '5,001-10,000', '10000+': '10,001+', '10001+': '10,001+'}

_OBSERVED_INTERVALS = ((1, '0-1'), (10, '2-10'), (50, '11-50'), (200, '51-200'), (500, '201-500'), (1000, '501-1,000'), (5000, '1,001-5,000'), (10000, '5,001-10,000'))

def normalize_employee_count_bucket(value: Any, *, default: str | None=DEFAULT_EMPLOYEE_BUCKET) -> str:
    if isinstance(value, Sequence) and (not isinstance(value, (str, bytes, bytearray))):
        for item in value:
            normalized = normalize_employee_count_bucket(item, default=None)
            if normalized:
                return normalized
        return str(default or '')
    raw = ' '.join(str(value or '').strip().split())
    if raw in LINKEDIN_EMPLOYEE_BUCKETS:
        return raw
    cleaned = raw.lower().replace('employees', '').replace('employee', '').replace(',', '').replace(' ', '').strip()
    for bucket in LINKEDIN_EMPLOYEE_BUCKETS:
        if cleaned == bucket.lower().replace(',', '').replace(' ', ''):
            return bucket
    return str(LEGACY_EMPLOYEE_BUCKET_MAP.get(cleaned) or LEGACY_EMPLOYEE_BUCKET_MAP.get(raw) or default or '')

def normalize_observed_employee_count_bucket(value: Any, *, default: str | None=None) -> str:
    if isinstance(value, bool):
        return str(default or '')
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and re.fullmatch('(?:0|[1-9][0-9]*)', value):
        if len(value) > 5:
            return '10,001+'
        count = int(value)
    else:
        return str(default or '')
    if count < 0:
        return str(default or '')
    for (maximum, bucket) in _OBSERVED_INTERVALS:
        if count <= maximum:
            return bucket
    return '10,001+'

# Deterministic helpers from qualification/scoring/country_data.py

COUNTRIES: tuple = (('Andorra', 'AD', 'AND', 'EU'), ('United Arab Emirates', 'AE', 'ARE', 'AS'), ('Afghanistan', 'AF', 'AFG', 'AS'), ('Antigua and Barbuda', 'AG', 'ATG', 'NA'), ('Anguilla', 'AI', 'AIA', 'NA'), ('Albania', 'AL', 'ALB', 'EU'), ('Armenia', 'AM', 'ARM', 'AS'), ('Netherlands Antilles', 'AN', 'ANT', 'NA'), ('Angola', 'AO', 'AGO', 'AF'), ('Antarctica', 'AQ', 'ATA', 'AN'), ('Argentina', 'AR', 'ARG', 'SA'), ('American Samoa', 'AS', 'ASM', 'OC'), ('Austria', 'AT', 'AUT', 'EU'), ('Australia', 'AU', 'AUS', 'OC'), ('Aruba', 'AW', 'ABW', 'NA'), ('Aland Islands', 'AX', 'ALA', 'EU'), ('Azerbaijan', 'AZ', 'AZE', 'AS'), ('Bosnia and Herzegovina', 'BA', 'BIH', 'EU'), ('Barbados', 'BB', 'BRB', 'NA'), ('Bangladesh', 'BD', 'BGD', 'AS'), ('Belgium', 'BE', 'BEL', 'EU'), ('Burkina Faso', 'BF', 'BFA', 'AF'), ('Bulgaria', 'BG', 'BGR', 'EU'), ('Bahrain', 'BH', 'BHR', 'AS'), ('Burundi', 'BI', 'BDI', 'AF'), ('Benin', 'BJ', 'BEN', 'AF'), ('Saint Barthelemy', 'BL', 'BLM', 'NA'), ('Bermuda', 'BM', 'BMU', 'NA'), ('Brunei', 'BN', 'BRN', 'AS'), ('Bolivia', 'BO', 'BOL', 'SA'), ('Bonaire, Saint Eustatius and Saba', 'BQ', 'BES', 'NA'), ('Brazil', 'BR', 'BRA', 'SA'), ('Bahamas', 'BS', 'BHS', 'NA'), ('Bhutan', 'BT', 'BTN', 'AS'), ('Bouvet Island', 'BV', 'BVT', 'AN'), ('Botswana', 'BW', 'BWA', 'AF'), ('Belarus', 'BY', 'BLR', 'EU'), ('Belize', 'BZ', 'BLZ', 'NA'), ('Canada', 'CA', 'CAN', 'NA'), ('Cocos Islands', 'CC', 'CCK', 'AS'), ('Democratic Republic of the Congo', 'CD', 'COD', 'AF'), ('Central African Republic', 'CF', 'CAF', 'AF'), ('Republic of the Congo', 'CG', 'COG', 'AF'), ('Switzerland', 'CH', 'CHE', 'EU'), ('Ivory Coast', 'CI', 'CIV', 'AF'), ('Cook Islands', 'CK', 'COK', 'OC'), ('Chile', 'CL', 'CHL', 'SA'), ('Cameroon', 'CM', 'CMR', 'AF'), ('China', 'CN', 'CHN', 'AS'), ('Colombia', 'CO', 'COL', 'SA'), ('Costa Rica', 'CR', 'CRI', 'NA'), ('Serbia and Montenegro', 'CS', 'SCG', 'EU'), ('Cuba', 'CU', 'CUB', 'NA'), ('Cabo Verde', 'CV', 'CPV', 'AF'), ('Curacao', 'CW', 'CUW', 'NA'), ('Christmas Island', 'CX', 'CXR', 'OC'), ('Cyprus', 'CY', 'CYP', 'EU'), ('Czechia', 'CZ', 'CZE', 'EU'), ('Germany', 'DE', 'DEU', 'EU'), ('Djibouti', 'DJ', 'DJI', 'AF'), ('Denmark', 'DK', 'DNK', 'EU'), ('Dominica', 'DM', 'DMA', 'NA'), ('Dominican Republic', 'DO', 'DOM', 'NA'), ('Algeria', 'DZ', 'DZA', 'AF'), ('Ecuador', 'EC', 'ECU', 'SA'), ('Estonia', 'EE', 'EST', 'EU'), ('Egypt', 'EG', 'EGY', 'AF'), ('Western Sahara', 'EH', 'ESH', 'AF'), ('Eritrea', 'ER', 'ERI', 'AF'), ('Spain', 'ES', 'ESP', 'EU'), ('Ethiopia', 'ET', 'ETH', 'AF'), ('Finland', 'FI', 'FIN', 'EU'), ('Fiji', 'FJ', 'FJI', 'OC'), ('Falkland Islands', 'FK', 'FLK', 'SA'), ('Micronesia', 'FM', 'FSM', 'OC'), ('Faroe Islands', 'FO', 'FRO', 'EU'), ('France', 'FR', 'FRA', 'EU'), ('Gabon', 'GA', 'GAB', 'AF'), ('United Kingdom', 'GB', 'GBR', 'EU'), ('Grenada', 'GD', 'GRD', 'NA'), ('Georgia', 'GE', 'GEO', 'AS'), ('French Guiana', 'GF', 'GUF', 'SA'), ('Guernsey', 'GG', 'GGY', 'EU'), ('Ghana', 'GH', 'GHA', 'AF'), ('Gibraltar', 'GI', 'GIB', 'EU'), ('Greenland', 'GL', 'GRL', 'NA'), ('Gambia', 'GM', 'GMB', 'AF'), ('Guinea', 'GN', 'GIN', 'AF'), ('Guadeloupe', 'GP', 'GLP', 'NA'), ('Equatorial Guinea', 'GQ', 'GNQ', 'AF'), ('Greece', 'GR', 'GRC', 'EU'), ('South Georgia and the South Sandwich Islands', 'GS', 'SGS', 'AN'), ('Guatemala', 'GT', 'GTM', 'NA'), ('Guam', 'GU', 'GUM', 'OC'), ('Guinea-Bissau', 'GW', 'GNB', 'AF'), ('Guyana', 'GY', 'GUY', 'SA'), ('Hong Kong', 'HK', 'HKG', 'AS'), ('Heard Island and McDonald Islands', 'HM', 'HMD', 'AN'), ('Honduras', 'HN', 'HND', 'NA'), ('Croatia', 'HR', 'HRV', 'EU'), ('Haiti', 'HT', 'HTI', 'NA'), ('Hungary', 'HU', 'HUN', 'EU'), ('Indonesia', 'ID', 'IDN', 'AS'), ('Ireland', 'IE', 'IRL', 'EU'), ('Israel', 'IL', 'ISR', 'AS'), ('Isle of Man', 'IM', 'IMN', 'EU'), ('India', 'IN', 'IND', 'AS'), ('British Indian Ocean Territory', 'IO', 'IOT', 'AS'), ('Iraq', 'IQ', 'IRQ', 'AS'), ('Iran', 'IR', 'IRN', 'AS'), ('Iceland', 'IS', 'ISL', 'EU'), ('Italy', 'IT', 'ITA', 'EU'), ('Jersey', 'JE', 'JEY', 'EU'), ('Jamaica', 'JM', 'JAM', 'NA'), ('Jordan', 'JO', 'JOR', 'AS'), ('Japan', 'JP', 'JPN', 'AS'), ('Kenya', 'KE', 'KEN', 'AF'), ('Kyrgyzstan', 'KG', 'KGZ', 'AS'), ('Cambodia', 'KH', 'KHM', 'AS'), ('Kiribati', 'KI', 'KIR', 'OC'), ('Comoros', 'KM', 'COM', 'AF'), ('Saint Kitts and Nevis', 'KN', 'KNA', 'NA'), ('North Korea', 'KP', 'PRK', 'AS'), ('South Korea', 'KR', 'KOR', 'AS'), ('Kuwait', 'KW', 'KWT', 'AS'), ('Cayman Islands', 'KY', 'CYM', 'NA'), ('Kazakhstan', 'KZ', 'KAZ', 'AS'), ('Laos', 'LA', 'LAO', 'AS'), ('Lebanon', 'LB', 'LBN', 'AS'), ('Saint Lucia', 'LC', 'LCA', 'NA'), ('Liechtenstein', 'LI', 'LIE', 'EU'), ('Sri Lanka', 'LK', 'LKA', 'AS'), ('Liberia', 'LR', 'LBR', 'AF'), ('Lesotho', 'LS', 'LSO', 'AF'), ('Lithuania', 'LT', 'LTU', 'EU'), ('Luxembourg', 'LU', 'LUX', 'EU'), ('Latvia', 'LV', 'LVA', 'EU'), ('Libya', 'LY', 'LBY', 'AF'), ('Morocco', 'MA', 'MAR', 'AF'), ('Monaco', 'MC', 'MCO', 'EU'), ('Moldova', 'MD', 'MDA', 'EU'), ('Montenegro', 'ME', 'MNE', 'EU'), ('Saint Martin', 'MF', 'MAF', 'NA'), ('Madagascar', 'MG', 'MDG', 'AF'), ('Marshall Islands', 'MH', 'MHL', 'OC'), ('North Macedonia', 'MK', 'MKD', 'EU'), ('Mali', 'ML', 'MLI', 'AF'), ('Myanmar', 'MM', 'MMR', 'AS'), ('Mongolia', 'MN', 'MNG', 'AS'), ('Macao', 'MO', 'MAC', 'AS'), ('Northern Mariana Islands', 'MP', 'MNP', 'OC'), ('Martinique', 'MQ', 'MTQ', 'NA'), ('Mauritania', 'MR', 'MRT', 'AF'), ('Montserrat', 'MS', 'MSR', 'NA'), ('Malta', 'MT', 'MLT', 'EU'), ('Mauritius', 'MU', 'MUS', 'AF'), ('Maldives', 'MV', 'MDV', 'AS'), ('Malawi', 'MW', 'MWI', 'AF'), ('Mexico', 'MX', 'MEX', 'NA'), ('Malaysia', 'MY', 'MYS', 'AS'), ('Mozambique', 'MZ', 'MOZ', 'AF'), ('Namibia', 'NA', 'NAM', 'AF'), ('New Caledonia', 'NC', 'NCL', 'OC'), ('Niger', 'NE', 'NER', 'AF'), ('Norfolk Island', 'NF', 'NFK', 'OC'), ('Nigeria', 'NG', 'NGA', 'AF'), ('Nicaragua', 'NI', 'NIC', 'NA'), ('The Netherlands', 'NL', 'NLD', 'EU'), ('Norway', 'NO', 'NOR', 'EU'), ('Nepal', 'NP', 'NPL', 'AS'), ('Nauru', 'NR', 'NRU', 'OC'), ('Niue', 'NU', 'NIU', 'OC'), ('New Zealand', 'NZ', 'NZL', 'OC'), ('Oman', 'OM', 'OMN', 'AS'), ('Panama', 'PA', 'PAN', 'NA'), ('Peru', 'PE', 'PER', 'SA'), ('French Polynesia', 'PF', 'PYF', 'OC'), ('Papua New Guinea', 'PG', 'PNG', 'OC'), ('Philippines', 'PH', 'PHL', 'AS'), ('Pakistan', 'PK', 'PAK', 'AS'), ('Poland', 'PL', 'POL', 'EU'), ('Saint Pierre and Miquelon', 'PM', 'SPM', 'NA'), ('Pitcairn', 'PN', 'PCN', 'OC'), ('Puerto Rico', 'PR', 'PRI', 'NA'), ('Palestinian Territory', 'PS', 'PSE', 'AS'), ('Portugal', 'PT', 'PRT', 'EU'), ('Palau', 'PW', 'PLW', 'OC'), ('Paraguay', 'PY', 'PRY', 'SA'), ('Qatar', 'QA', 'QAT', 'AS'), ('Reunion', 'RE', 'REU', 'AF'), ('Romania', 'RO', 'ROU', 'EU'), ('Serbia', 'RS', 'SRB', 'EU'), ('Russia', 'RU', 'RUS', 'EU'), ('Rwanda', 'RW', 'RWA', 'AF'), ('Saudi Arabia', 'SA', 'SAU', 'AS'), ('Solomon Islands', 'SB', 'SLB', 'OC'), ('Seychelles', 'SC', 'SYC', 'AF'), ('Sudan', 'SD', 'SDN', 'AF'), ('Sweden', 'SE', 'SWE', 'EU'), ('Singapore', 'SG', 'SGP', 'AS'), ('Saint Helena', 'SH', 'SHN', 'AF'), ('Slovenia', 'SI', 'SVN', 'EU'), ('Svalbard and Jan Mayen', 'SJ', 'SJM', 'EU'), ('Slovakia', 'SK', 'SVK', 'EU'), ('Sierra Leone', 'SL', 'SLE', 'AF'), ('San Marino', 'SM', 'SMR', 'EU'), ('Senegal', 'SN', 'SEN', 'AF'), ('Somalia', 'SO', 'SOM', 'AF'), ('Suriname', 'SR', 'SUR', 'SA'), ('South Sudan', 'SS', 'SSD', 'AF'), ('Sao Tome and Principe', 'ST', 'STP', 'AF'), ('El Salvador', 'SV', 'SLV', 'NA'), ('Sint Maarten', 'SX', 'SXM', 'NA'), ('Syria', 'SY', 'SYR', 'AS'), ('Eswatini', 'SZ', 'SWZ', 'AF'), ('Turks and Caicos Islands', 'TC', 'TCA', 'NA'), ('Chad', 'TD', 'TCD', 'AF'), ('French Southern Territories', 'TF', 'ATF', 'AN'), ('Togo', 'TG', 'TGO', 'AF'), ('Thailand', 'TH', 'THA', 'AS'), ('Tajikistan', 'TJ', 'TJK', 'AS'), ('Tokelau', 'TK', 'TKL', 'OC'), ('Timor Leste', 'TL', 'TLS', 'OC'), ('Turkmenistan', 'TM', 'TKM', 'AS'), ('Tunisia', 'TN', 'TUN', 'AF'), ('Tonga', 'TO', 'TON', 'OC'), ('Turkey', 'TR', 'TUR', 'AS'), ('Trinidad and Tobago', 'TT', 'TTO', 'NA'), ('Tuvalu', 'TV', 'TUV', 'OC'), ('Taiwan', 'TW', 'TWN', 'AS'), ('Tanzania', 'TZ', 'TZA', 'AF'), ('Ukraine', 'UA', 'UKR', 'EU'), ('Uganda', 'UG', 'UGA', 'AF'), ('United States Minor Outlying Islands', 'UM', 'UMI', 'OC'), ('United States', 'US', 'USA', 'NA'), ('Uruguay', 'UY', 'URY', 'SA'), ('Uzbekistan', 'UZ', 'UZB', 'AS'), ('Vatican', 'VA', 'VAT', 'EU'), ('Saint Vincent and the Grenadines', 'VC', 'VCT', 'NA'), ('Venezuela', 'VE', 'VEN', 'SA'), ('British Virgin Islands', 'VG', 'VGB', 'NA'), ('U.S. Virgin Islands', 'VI', 'VIR', 'NA'), ('Vietnam', 'VN', 'VNM', 'AS'), ('Vanuatu', 'VU', 'VUT', 'OC'), ('Wallis and Futuna', 'WF', 'WLF', 'OC'), ('Samoa', 'WS', 'WSM', 'OC'), ('Kosovo', 'XK', 'XKX', 'EU'), ('Yemen', 'YE', 'YEM', 'AS'), ('Mayotte', 'YT', 'MYT', 'AF'), ('South Africa', 'ZA', 'ZAF', 'AF'), ('Zambia', 'ZM', 'ZMB', 'AF'), ('Zimbabwe', 'ZW', 'ZWE', 'AF'))

US_STATES: dict = {'alaska': 'Alaska', 'AK': 'Alaska', 'alabama': 'Alabama', 'AL': 'Alabama', 'arkansas': 'Arkansas', 'AR': 'Arkansas', 'american samoa': 'American Samoa', 'AS': 'American Samoa', 'arizona': 'Arizona', 'AZ': 'Arizona', 'california': 'California', 'CA': 'California', 'colorado': 'Colorado', 'CO': 'Colorado', 'connecticut': 'Connecticut', 'CT': 'Connecticut', 'delaware': 'Delaware', 'DE': 'Delaware', 'florida': 'Florida', 'FL': 'Florida', 'georgia': 'Georgia', 'GA': 'Georgia', 'guam': 'Guam', 'GU': 'Guam', 'hawaii': 'Hawaii', 'HI': 'Hawaii', 'iowa': 'Iowa', 'IA': 'Iowa', 'idaho': 'Idaho', 'ID': 'Idaho', 'illinois': 'Illinois', 'IL': 'Illinois', 'indiana': 'Indiana', 'IN': 'Indiana', 'kansas': 'Kansas', 'KS': 'Kansas', 'kentucky': 'Kentucky', 'KY': 'Kentucky', 'louisiana': 'Louisiana', 'LA': 'Louisiana', 'massachusetts': 'Massachusetts', 'MA': 'Massachusetts', 'maryland': 'Maryland', 'MD': 'Maryland', 'maine': 'Maine', 'ME': 'Maine', 'michigan': 'Michigan', 'MI': 'Michigan', 'minnesota': 'Minnesota', 'MN': 'Minnesota', 'missouri': 'Missouri', 'MO': 'Missouri', 'northern mariana islands': 'Northern Mariana Islands', 'MP': 'Northern Mariana Islands', 'mississippi': 'Mississippi', 'MS': 'Mississippi', 'montana': 'Montana', 'MT': 'Montana', 'north carolina': 'North Carolina', 'NC': 'North Carolina', 'north dakota': 'North Dakota', 'ND': 'North Dakota', 'nebraska': 'Nebraska', 'NE': 'Nebraska', 'new hampshire': 'New Hampshire', 'NH': 'New Hampshire', 'new jersey': 'New Jersey', 'NJ': 'New Jersey', 'new mexico': 'New Mexico', 'NM': 'New Mexico', 'nevada': 'Nevada', 'NV': 'Nevada', 'new york': 'New York', 'NY': 'New York', 'ohio': 'Ohio', 'OH': 'Ohio', 'oklahoma': 'Oklahoma', 'OK': 'Oklahoma', 'oregon': 'Oregon', 'OR': 'Oregon', 'pennsylvania': 'Pennsylvania', 'PA': 'Pennsylvania', 'puerto rico': 'Puerto Rico', 'PR': 'Puerto Rico', 'rhode island': 'Rhode Island', 'RI': 'Rhode Island', 'south carolina': 'South Carolina', 'SC': 'South Carolina', 'south dakota': 'South Dakota', 'SD': 'South Dakota', 'tennessee': 'Tennessee', 'TN': 'Tennessee', 'texas': 'Texas', 'TX': 'Texas', 'utah': 'Utah', 'UT': 'Utah', 'virginia': 'Virginia', 'VA': 'Virginia', 'virgin islands': 'Virgin Islands', 'VI': 'Virgin Islands', 'vermont': 'Vermont', 'VT': 'Vermont', 'washington': 'Washington', 'WA': 'Washington', 'wisconsin': 'Wisconsin', 'WI': 'Wisconsin', 'west virginia': 'West Virginia', 'WV': 'West Virginia', 'wyoming': 'Wyoming', 'WY': 'Wyoming'}

# Deterministic helpers from qualification/scoring/pre_checks.py

class ValidationResult(NamedTuple):
    """Result of a validation check."""
    passed: bool
    reason: Optional[str] = None

_COUNTRY_ALIASES: dict = {'usa': 'united states', 'us': 'united states', 'u.s.': 'united states', 'u.s.a.': 'united states', 'united states of america': 'united states', 'america': 'united states', 'uk': 'united kingdom', 'great britain': 'united kingdom', 'england': 'united kingdom', 'u.k.': 'united kingdom', 'scotland': 'united kingdom', 'wales': 'united kingdom', 'northern ireland': 'united kingdom', 'uae': 'united arab emirates', 'korea': 'south korea', 'republic of korea': 'south korea', 'korea, republic of': 'south korea', 'russian federation': 'russia', 'taiwan, province of china': 'taiwan', 'czech republic': 'czechia', 'holland': 'the netherlands', 'netherlands': 'the netherlands', 'deutschland': 'germany', 'turkiye': 'turkey', 'türkiye': 'turkey', 'viet nam': 'vietnam', "cote d'ivoire": 'ivory coast', "côte d'ivoire": 'ivory coast'}

_CONTINENT_CODES: dict = {'europe': 'EU', 'north america': 'NA', 'south america': 'SA', 'asia': 'AS', 'africa': 'AF', 'oceania': 'OC', 'antarctica': 'AN'}

_GEO_LOOKUP_CACHE: dict = {}

def _country_lookup() -> dict:
    """name/alias/ISO-code -> canonical country name.

    Built once per process from the vendored ISO-3166 table
    (qualification/scoring/country_data.py, generated by
    scripts/generate_country_data.py — no geo library in the hot path).
    Alpha-2/alpha-3 codes are stored uppercase and only matched against
    uppercase input, so lowercase English words in geography prose
    ("in", "it", "no", "and") never resolve as ISO codes.
    """
    cached = _GEO_LOOKUP_CACHE.get('countries')
    if cached is not None:
        return cached
    pass
    lookup: dict = {}
    continents: dict = {}
    for (name, iso2, iso3, continent) in COUNTRIES:
        canonical = name.lower()
        lookup[canonical] = canonical
        if iso2:
            lookup[iso2.upper()] = canonical
        if iso3:
            lookup[iso3.upper()] = canonical
        if continent:
            continents.setdefault(continent.upper(), set()).add(canonical)
    for (alias, target) in _COUNTRY_ALIASES.items():
        lookup[alias] = target
    _GEO_LOOKUP_CACHE['countries'] = lookup
    _GEO_LOOKUP_CACHE['continents'] = {code: frozenset(members) for (code, members) in continents.items()}
    return lookup

def _continent_members(code: str) -> frozenset:
    _country_lookup()
    return _GEO_LOOKUP_CACHE['continents'].get(code, frozenset())

def _resolve_country(value: str) -> Optional[str]:
    """Resolve free text to a canonical country name, or None."""
    token = str(value or '').strip()
    if not token:
        return None
    lookup = _country_lookup()
    resolved = lookup.get(token.lower())
    if resolved is not None:
        return resolved
    lowered = token.lower()
    if lowered.startswith('the ') and len(lowered) > 4:
        resolved = lookup.get(lowered[4:].strip())
        if resolved is not None:
            return resolved
    if len(token) in (2, 3) and token.isupper():
        return lookup.get(token)
    return None

def _normalize_country(name: str) -> str:
    """Normalize a country string to its canonical form for comparison."""
    return _resolve_country(name) or str(name or '').strip().lower()

_GEO_TOKEN_SPLIT = re.compile(',|/|&|\\bor\\b|\\band\\b', re.IGNORECASE)

def _allowed_countries_from_icp_geography(value: str) -> frozenset:
    """The set of canonical countries an ICP geography string permits.

    Handles the shapes production ICPs and client ICPs actually use:
    ``"United States, West Coast"`` (country + region),
    ``"London, United Kingdom"`` (city first), ``"United States or
    Canada"`` (multi-country), ``"Europe"`` (continent), and
    ``"Georgia, United States"`` (US state names that collide with
    country names — any US state in the string also permits the US).
    Returns an empty set when nothing resolves; the caller then defers
    geography to the ICP-fit scorer instead of hard-zeroing on a string
    that can never equal a country.
    """
    allowed: set = set()
    for raw_token in _GEO_TOKEN_SPLIT.split(str(value or '')):
        token = raw_token.strip()
        if not token:
            continue
        country = _resolve_country(token)
        if country is not None:
            allowed.add(country)
        continent_code = _CONTINENT_CODES.get(token.lower()) or ('EU' if token == 'EU' else None)
        if continent_code:
            allowed.update(_continent_members(continent_code))
        us_state = _lookup_us_state(token)
        if us_state is not None:
            allowed.add('united states')
    return frozenset(allowed)

def _lookup_us_state(token: str):
    """US state name for a token, or None; abbreviations must be uppercase."""
    pass
    text = token.strip()
    if not text:
        return None
    if len(text) == 2:
        return US_STATES.get(text) if text.isupper() else None
    return US_STATES.get(text.lower())

def check_country_match(lead_country: str, icp_country: str) -> ValidationResult:
    """
    Verify lead's country matches ICP requirement.

    Uses case-insensitive matching with common alias normalization
    (e.g., USA = United States, UK = United Kingdom).
    If the ICP doesn't specify a country, any country is accepted.
    """
    if not icp_country or not icp_country.strip():
        return ValidationResult(passed=True)
    if not lead_country or not lead_country.strip():
        return ValidationResult(passed=False, reason=f"Missing country (ICP requires '{icp_country}')")
    allowed = _allowed_countries_from_icp_geography(icp_country)
    if not allowed:
        geography_token = str(icp_country or '').strip()
        if len(geography_token) in (2, 3) and geography_token.isalpha() and geography_token.isupper():
            return ValidationResult(passed=False, reason=f"Country mismatch: ICP geography '{icp_country}' is not a recognized ISO country code")
        logger.info('Country pre-check deferred to ICP-fit scorer: ICP geography %r does not resolve to a recognized country', icp_country)
        return ValidationResult(passed=True)
    if _normalize_country(lead_country) not in allowed:
        return ValidationResult(passed=False, reason=f"Country mismatch: '{lead_country}' vs ICP '{icp_country}'")
    return ValidationResult(passed=True)

# Deterministic helpers from qualification/scoring/competition.py

class CompetitionScorerInputError(ValueError):
    """A company or ICP does not satisfy the public competition boundary."""

def employee_count_buckets_for_icp(icp: Mapping[str, Any]) -> list[str]:
    """Return the exact employee buckets declared by one ICP."""
    raw = icp.get('employee_count')
    values = list(raw) if isinstance(raw, Sequence) and (not isinstance(raw, (str, bytes, bytearray))) else str(raw or '').replace(';', '|').split('|')
    buckets: list[str] = []
    for value in values:
        bucket = normalize_employee_count_bucket(value, default=None)
        if bucket and bucket not in buckets:
            buckets.append(bucket)
    if not buckets:
        raise CompetitionScorerInputError('ICP employee_count has no valid bucket')
    return buckets

# Deterministic helpers from qualification/scoring/lead_scorer.py

def _submitted_employee_size_decision(company: CompanyOutput, icp: ICPPrompt) -> str:
    bucket = _normalize_linkedin_employee_bucket(company.employee_count)
    (targets, targets_verified) = _normalize_icp_employee_buckets(icp.employee_count)
    if not bucket or not targets_verified:
        return COMPANY_FIT_UNAVAILABLE
    return COMPANY_FIT_MATCH if bucket in targets else COMPANY_FIT_MISMATCH

def _submitted_geography_decision(company: CompanyOutput, icp: ICPPrompt) -> str:
    submitted = str(company.country or '').strip()
    requested = str(icp.country or icp.geography or '').strip()
    if not submitted or not requested:
        return COMPANY_FIT_UNAVAILABLE
    return COMPANY_FIT_MATCH if check_country_match(submitted, requested).passed else COMPANY_FIT_MISMATCH

def _submitted_stage_decision(company: CompanyOutput, icp: ICPPrompt) -> str:
    requested = _normalize_company_stage(icp.company_stage)
    if not requested:
        return COMPANY_FIT_MATCH
    submitted = _normalize_company_stage(company.company_stage)
    if not submitted:
        return COMPANY_FIT_UNAVAILABLE
    return COMPANY_FIT_MATCH if _company_stage_matches(submitted, requested) else COMPANY_FIT_MISMATCH

def _normalize_linkedin_employee_bucket(value) -> str:
    try:
        pass
        return normalize_employee_count_bucket(value, default=None)
    except Exception as e:
        logger.warning('competition employee bucket normalization failed: %s: %s', type(e).__name__, e)
        return ''

def _normalize_icp_employee_buckets(value) -> Tuple[set, bool]:
    """Return exact structured LinkedIn buckets and whether all were verified.

    Commas are thousands separators inside LinkedIn ranges, never list
    delimiters.  Splitting ``"501-1,000"`` on a comma silently removed the
    requested band and made the size gate fail open.  Lists remain structured;
    legacy strings may use only ``|``, ``;``, or the word ``or`` as separators.
    Known historical labels are canonicalized to the same exact buckets. Any
    missing, unknown, or malformed item makes the whole requirement unverified
    so it cannot match a candidate.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        pieces = [str(item).strip() for item in value if str(item).strip()]
    else:
        raw = str(value or '').strip()
        pieces = [item.strip() for item in re.split('\\s*(?:\\||;|\\bor\\b)\\s*', raw, flags=re.I) if item.strip()]
    if not pieces or any((piece.lower() in {'any', 'all', 'unknown', 'n/a', 'na'} for piece in pieces)):
        return (set(), False)
    try:
        pass
    except Exception as e:
        logger.warning('competition ICP employee enum loading failed: %s: %s', type(e).__name__, e)
        return (set(), False)
    canonical = set(LINKEDIN_EMPLOYEE_BUCKETS)
    normalized = [normalize_employee_count_bucket(piece, default=None) for piece in pieces]
    if any((not bucket or bucket not in canonical for bucket in normalized)):
        return (set(), False)
    return (set(normalized), True)

def _normalize_company_stage(value) -> str:
    text = str(value or '').strip().lower()
    if not text or text in {'any', 'all', 'unknown', 'n/a', 'na', 'not specified'}:
        return ''
    if re.fullmatch('series\\s*c\\s*\\+', text):
        return 'series c+'
    text = re.sub('[^a-z0-9]+', ' ', text)
    return ' '.join(text.split())

_SERIES_C_PLUS_MATCHING_STAGES = frozenset({'series c+', 'series c', 'series d', 'series e', 'series f', 'series g', 'series h'})

def _company_stage_matches(observed: str, requested: str) -> bool:
    """Apply the model-owned closed Series C+ category during scoring."""
    if observed == requested:
        return True
    return requested == 'series c+' and observed in _SERIES_C_PLUS_MATCHING_STAGES

# Deterministic helpers from qualification/scoring/intent_signal_gate.py

_INVALID_URL_PATTERNS = ['/alternatives(?:\\b|/|\\?|$)', '/competitors(?:\\b|/|\\?|$)', 'indeed\\.com/hire/job-description/', 'github\\.com/[^/]+/[^/]+/labels(?:/|$)', 'github\\.com/[^/]+/[^/]+/discussions/\\d+(?:/|$)']

_INVALID_URL_RE = re.compile('|'.join(_INVALID_URL_PATTERNS), re.IGNORECASE)

def check_url_structural_validity(url: str) -> Optional[str]:
    """Return a rejection reason if the URL path cannot be valid evidence.

    Returns None for any URL whose path is structurally acceptable; the URL
    may still fail later content-based checks.
    """
    if not url:
        return None
    match = _INVALID_URL_RE.search(url)
    if match:
        return f"URL path '{match.group()}' is not a valid intent-evidence source (aggregator / template / repo metadata)"
    return None

_ANTIBOT_PATTERNS = ['access denied', 'verifying your connection', 'verifying.{0,30}browser', 'just a moment', 'enable javascript', 'please enable js', 'additional verification required', 'verifying you are human', 'sign in to (?:linkedin|see|join|view|continue)', 'join linkedin to', 'create an account to (?:see|join)', 'page can.?t be found', 'this page (?:doesn.?t|does not) exist', '403\\s*[-:|—]?\\s*forbidden', '404\\s*[-:|—]?\\s*(?:not\\s*found|page.*not.*found)', 'this content isn.?t available']

_ANTIBOT_RE = re.compile('|'.join(_ANTIBOT_PATTERNS), re.IGNORECASE)

_ANTIBOT_MAX_LEN = 4000

def check_antibot_wall(content: str) -> Optional[str]:
    """Return a rejection reason if the fetched page is a bot challenge or
    login wall rather than real content."""
    if not content:
        return None
    head = content[:5000].lower()
    match = _ANTIBOT_RE.search(head)
    if match and len(content) < _ANTIBOT_MAX_LEN:
        return f'Page returned anti-bot / login wall (matched pattern: {match.group()[:40]!r}) — cannot verify claim from this content'
    return None

_FRESHNESS_WINDOWS = {'in the last few weeks': 60, 'in the last 30 days': 45, 'in the last 60 days': 75, 'in the last 90 days': 105, 'in the last 6 months': 200, 'in the last 12 months': 400, 'in the past few weeks': 60, 'in the past 30 days': 45, 'in the past 60 days': 75, 'in the past 90 days': 105, 'in the past 6 months': 200, 'in the past 12 months': 400, 'last few weeks': 60, 'last 30 days': 45, 'last 60 days': 75, 'last 90 days': 105, 'last 6 months': 200, 'last 12 months': 400, 'past 30 days': 45, 'past 60 days': 75, 'past 90 days': 105, 'past 6 months': 200, 'past 12 months': 400, 'recently': 180}

def _claim_max_age_days(claim_text: str) -> Optional[int]:
    """Return the tightest matching freshness window for the claim, or None
    if the claim has no time bound."""
    if not claim_text:
        return None
    lowered = claim_text.lower()
    best: Optional[int] = None
    for (phrase, days) in _FRESHNESS_WINDOWS.items():
        if phrase in lowered and (best is None or days < best):
            best = days
    return best

def _parse_signal_date(date_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 or YYYY-MM-DD date into an aware UTC datetime.

    Treats implausibly-old dates (pre-2000) as unparseable.  Miners
    occasionally emit ``1970-01-01`` (Unix epoch) as a "no verifiable date"
    sentinel instead of using the schema's ``null``; collapsing those to
    None lets the recency gate apply its proper "missing date" policy.
    """
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        try:
            parsed = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
    if parsed.year < 2000:
        return None
    return parsed

def check_evidence_freshness(claim_text: str, signal_date: Optional[str], content_found_date: Optional[str]=None, buyer_cap_days: Optional[int]=None) -> Optional[str]:
    """Return a rejection reason if evidence is older than the cap.

    When ``buyer_cap_days`` is set (operator-classified at request creation),
    it is the authoritative cap.  Falls back to phrase-scanning the claim
    text for legacy ICPs without an explicit cap.  Returns None when no
    cap can be derived, the date cannot be parsed, or the evidence falls
    within the window.
    """
    max_age = buyer_cap_days
    if max_age is None and claim_text:
        max_age = _claim_max_age_days(claim_text)
    if max_age is None:
        return None
    date_str = signal_date or content_found_date
    parsed = _parse_signal_date(date_str) if date_str else None
    if parsed is None:
        if buyer_cap_days is not None:
            return f'buyer requires evidence within {buyer_cap_days} days but signal has no valid date (got {date_str!r})'
        return None
    pass
    age_days = (evaluation_datetime() - parsed).days
    if age_days > max_age:
        return f"Signal date {date_str} is {age_days} days old, but claim's freshness window allows max {max_age} days"
    return None

def run_all_prechecks(signal: Dict[str, Any], page_content: Optional[str]=None) -> Optional[str]:
    """Run Layers 1-3 in order and return the first rejection reason.

    Args:
        signal: Signal record with at minimum a ``url`` key and either a
            ``matched_icp_signal`` or ``description`` carrying the claim text.
            ``date`` and ``content_found_date`` are consulted by the freshness
            check.
        page_content: HTML-stripped page text used by the anti-bot check.
            Optional; if omitted, the anti-bot layer is skipped.

    Returns:
        The first rejection reason as a human-readable string, or None if
        every pre-check passes.
    """
    reason = check_url_structural_validity(signal.get('url') or '')
    if reason:
        return reason
    reason = check_evidence_freshness(signal.get('matched_icp_signal') or signal.get('description') or '', signal.get('date'), signal.get('content_found_date'))
    if reason:
        return reason
    if page_content:
        reason = check_antibot_wall(page_content)
        if reason:
            return reason
    return None

# Deterministic helpers from qualification/scoring/verification_helpers.py

def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    t = text.lower()
    t = re.sub('[^\\w\\s]', ' ', t)
    t = re.sub('\\s+', ' ', t).strip()
    return t

def compute_snippet_overlap(snippet: str, content: str) -> float:
    """
    Compute what fraction of a snippet's 4-word n-grams appear in the content.

    Returns a float 0.0-1.0 representing the overlap ratio. Legitimate models
    that extract verbatim text from web pages will score near 1.0. Models that
    fabricate, template, or strip/modify text will score much lower.
    """
    norm_snippet = _normalize_text(snippet)
    norm_content = _normalize_text(content)
    snippet_words = norm_snippet.split()
    if len(snippet_words) < 4:
        return 1.0
    content_set: set = set()
    content_words = norm_content.split()
    for i in range(len(content_words) - 3):
        content_set.add(tuple(content_words[i:i + 4]))
    matches = 0
    total = len(snippet_words) - 3
    for i in range(total):
        if tuple(snippet_words[i:i + 4]) in content_set:
            matches += 1
    return matches / total if total > 0 else 1.0

_SIGNAL_WORDS = {'launched', 'announced', 'expanded', 'expanding', 'partnered', 'partnership', 'merged', 'acquisition', 'acquired', 'hired', 'hiring', 'recruited', 'recruiting', 'opening', 'openings', 'funding', 'funded', 'raised', 'secured', 'closed', 'obtained', 'invested', 'investment', 'seed', 'series'}

def check_snippet_signal_grounding(snippet: str, source_content: str) -> tuple:
    """
    Check whether intent signal words in the SNIPPET actually appear in the source.

    Mirror of ``check_signal_word_grounding`` for the snippet field. Catches the
    pattern observed in the 2026-05-12 Risotto false positive: a miner glues
    fabricated prose ("Recently secured seed funding to support product
    development") onto a real chunk of scraped content (a GitHub labels JSON
    payload). The 4-gram snippet-overlap check passes because the labels JSON
    dominates the snippet by length, but the fabricated funding claim never
    appears on the actual page.

    Returns ``(grounded_count, total_signal_words, ungrounded_words)``. Callers
    treat ``total_signal_words > 0 and grounded_count == 0`` as a hard reject
    (same posture as the description-side check) — if EVERY funding/hiring
    word in the snippet is missing from the page, the snippet is fabricated.

    We intentionally do NOT reject when the snippet mentions e.g.
    "expanded into Europe" and the source page only has "expanded their team"
    — partial grounding is enough to confirm the snippet is anchored in the
    page rather than entirely invented.
    """
    content_lower = _normalize_text(source_content)
    content_words = set(content_lower.split())
    snip_lower = _normalize_text(snippet)
    snip_words = set(snip_lower.split())
    signal_in_snip = snip_words & _SIGNAL_WORDS
    if not signal_in_snip:
        return (0, 0, [])
    grounded = signal_in_snip & content_words
    ungrounded = signal_in_snip - content_words
    return (len(grounded), len(signal_in_snip), sorted(ungrounded))

# Deterministic helpers from qualification/scoring/company_fit_decision.py

COMPANY_FIT_MATCH = 'match'

COMPANY_FIT_MISMATCH = 'mismatch'

COMPANY_FIT_UNAVAILABLE = 'unavailable'

_LEGAL_SUFFIXES: Final = frozenset({'incorporated', 'corporation', 'company', 'limited', 'holdings', 'group', 'inc', 'corp', 'co', 'llc', 'ltd', 'plc', 'gmbh', 'ag', 'sa', 'nv', 'bv', 'oy', 'ab', 'as', 'pty', 'pte', 'kk', 'srl', 'spa'})

_LINKEDIN_SLUG_RE = re.compile('^[a-z0-9][a-z0-9._%+-]{0,99}$')

_PARENTHETICAL_INITIALISM_RE = re.compile('^\\s*(?P<legal_name>.+?)\\s*\\(\\s*(?P<initialism>[A-Z0-9]{2,10})\\s*\\)\\s*$')

_INDEPENDENT_IDENTITY_SOURCES: Final = frozenset({'company_homepage', 'company_web_reverification'})

def _canonical_domain(value: Any) -> str:
    raw = str(value or '').strip()
    if not raw or any((character.isspace() for character in raw)):
        return ''
    if '://' not in raw and (not raw.startswith('//')):
        raw = f'//{raw}'
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ''
    if parsed.username or parsed.password:
        return ''
    host = (parsed.hostname or '').casefold().removeprefix('www.')
    if not host or '.' not in host or host.endswith('.') or (':' in host):
        return ''
    try:
        return host.encode('idna').decode('ascii')
    except UnicodeError:
        return ''

def _linkedin_slug(value: Any) -> str:
    raw = str(value or '').strip()
    if not raw:
        return ''
    if '://' not in raw:
        raw = f'https://{raw}'
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ''
    host = (parsed.hostname or '').casefold().removeprefix('www.')
    parts = [part for part in parsed.path.split('/') if part]
    if not (host == 'linkedin.com' or host.endswith('.linkedin.com')) or len(parts) < 2 or parts[0].casefold() != 'company':
        return ''
    slug = parts[1].casefold()
    return slug if _LINKEDIN_SLUG_RE.fullmatch(slug) else ''

def _company_name(value: Any) -> str:
    """Normalize a company name after removing terminal legal suffixes only.

    Some legal-looking tokens are also real leading name terms: ``Group Nine
    Media`` and ``AG Grid`` must not collapse into different companies merely
    because ``group`` and ``ag`` can occur as legal suffixes elsewhere.
    """
    words = re.findall('[a-z0-9]+', str(value or '').casefold())
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return ''.join(words)

def _verified_parenthetical_name_alignment(submitted_name: Any, observed_name: Any, *, observed_linkedin_slug: str, evidence_source: str) -> bool:
    """Recognize an explicit legal-name/common-initialism pair.

    This narrow bridge applies only to independently observed identity evidence
    on the exact submitted domain. It does not infer unstated or fuzzy aliases.
    """
    if not observed_linkedin_slug or evidence_source not in _INDEPENDENT_IDENTITY_SOURCES:
        return False
    match = _PARENTHETICAL_INITIALISM_RE.fullmatch(str(submitted_name or '').strip())
    if match is None:
        return False
    initialism = match.group('initialism').casefold()
    if observed_linkedin_slug != initialism:
        return False
    return _company_name(match.group('legal_name')) == _company_name(observed_name)

def evaluate_company_identity(*, submitted_name: Any, submitted_website: Any, submitted_linkedin: Any, observed_name: Any, observed_website: Any, observed_linkedin: Any='', evidence_source: Any='') -> dict[str, str]:
    """Bind the submitted name, website, and LinkedIn to one observed entity."""
    submitted_linkedin_raw = str(submitted_linkedin or '').strip()
    submitted = {'name': _company_name(submitted_name), 'domain': _canonical_domain(submitted_website), 'linkedin_slug': _linkedin_slug(submitted_linkedin)}
    observed = {'name': _company_name(observed_name), 'domain': _canonical_domain(observed_website), 'linkedin_slug': _linkedin_slug(observed_linkedin)}
    source = str(evidence_source or '').strip().casefold()
    receipt = {'decision': 'unavailable', 'reason_code': 'identity_not_proven', 'submitted_name': submitted['name'], 'submitted_domain': submitted['domain'], 'submitted_linkedin_slug': submitted['linkedin_slug'], 'observed_name': observed['name'], 'observed_domain': observed['domain'], 'observed_linkedin_slug': observed['linkedin_slug'], 'evidence_source': source}
    if not submitted['name'] or not submitted['domain']:
        receipt.update(decision='mismatch', reason_code='identity_unresolved')
        return receipt
    if submitted_linkedin_raw and (not submitted['linkedin_slug']):
        receipt.update(decision='mismatch', reason_code='identity_unresolved')
        return receipt
    if not observed['name'] or not observed['domain']:
        return receipt
    if observed['domain'] != submitted['domain']:
        receipt.update(decision='mismatch', reason_code='identity_mismatch')
        return receipt
    if submitted['linkedin_slug'] and (not observed['linkedin_slug']):
        return receipt
    if submitted['linkedin_slug'] and observed['linkedin_slug'] != submitted['linkedin_slug']:
        if observed['linkedin_slug'].isdigit() != submitted['linkedin_slug'].isdigit():
            receipt.update(reason_code='identity_linkedin_alias_unresolved')
            return receipt
        if source == 'company_homepage' and observed['name'] == submitted['name'] and observed['linkedin_slug'] and (not observed['linkedin_slug'].isdigit()):
            receipt.update(reason_code='identity_linkedin_alias_unresolved')
            return receipt
        receipt.update(decision='mismatch', reason_code='identity_mismatch')
        return receipt
    if observed['name'] != submitted['name']:
        parenthetical_names_align = _verified_parenthetical_name_alignment(submitted_name, observed_name, observed_linkedin_slug=observed['linkedin_slug'], evidence_source=source)
        if submitted['linkedin_slug']:
            if not parenthetical_names_align:
                shorter_name = min((submitted['name'], observed['name']), key=len)
                longer_name = max((submitted['name'], observed['name']), key=len)
                if len(shorter_name) < 4 or not longer_name.startswith(shorter_name):
                    receipt.update(decision='mismatch', reason_code='identity_mismatch')
                    return receipt
        elif source != 'company_homepage' or not parenthetical_names_align:
            return receipt
    receipt.update(decision='match', reason_code='verifier_accepted')
    return receipt
