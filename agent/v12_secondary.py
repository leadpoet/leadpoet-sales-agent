"""Stage uncertainty and a shared-quota candidate completion reservation."""
from contextvars import ContextVar
import re
from agent.deadline import BudgetExhausted
from agent.v12_llm import enabled

ACTIVE = ContextVar('candidate_completion', default=None)
# Candidate work is serial. Keep four physical requests for post-primary
# country/stage/size completion. Cached pages and optional hints cost no lease.
TAIL = {'deepline': 4}
ENTRY = {'deepline': 6}


def observed_enumeration(text):
    if not enabled('V12_STAGE_ENUMERATION'):
        return ''
    part = r'(?:pre[ -]?seed|seed|series\s+[a-h]\+?)'
    match = re.search(r'(?<!\w)' + part + r'(?:\s*(?:,|/|&|\band\b|\bor\b)\s*' + part + r')+(?!\w)', str(text), re.I)
    return match.group(0) if match else ''


def stage_rank(stage):
    if stage == 'Debt':
        return 21
    if stage == 'Private Equity':
        return 20
    rounds = re.findall(r'(?i)series\s+([a-h])', stage)
    return max((ord(c.upper())-ord('A')+2 for c in rounds), default=1 if stage.lower() == 'seed' else 0)


def enumeration_matches(observed, wanted):
    """Only bare round enumerations; prose/chronology is deliberately excluded."""
    if not enabled('V12_STAGE_ENUMERATION'):
        return False
    round_pattern = r'(?:pre[ -]?seed|seed|series\s+[a-h]\+?)'
    text = str(observed).strip().lower()
    if not re.fullmatch(round_pattern + r'(?:\s*(?:,|/|&|\band\b|\bor\b)\s*' + round_pattern + r')+', text):
        return False
    rounds = re.findall(round_pattern, text)
    normalize = lambda s: re.sub(r'[^a-z0-9+]', '', s.lower())
    target = normalize(wanted)
    return any(normalize(value) == target or (target == 'seriesc+' and normalize(value) in
                                            {'series' + c for c in 'cdefgh'}) for value in rounds)


def unconfirmed_allowed():
    return enabled('V12_STAGE_UNPROVEN')


class CompletionLease:
    """Serial candidate ownership plus an untouchable pre-primary tail budget.

    The pipeline uses one candidate slot with this experiment enabled. Earlier
    candidates cannot race a new candidate for reserved provider calls. No
    credits/calls are pre-spent; the physical atomic counter enforces 30.
    """
    def __init__(self, run, company, trace):
        self.company, self.trace, self.finishing = company, trace, False
        available = {k: run.budget[k] - run.used[k] for k in ENTRY}
        if any(available[k] < ENTRY[k] for k in ENTRY):
            raise BudgetExhausted('candidate completion admission budget')
        self.token = ACTIVE.set(self)
        trace('completion.reserve', {'company': company, 'tail': TAIL, 'available': available})

    def before_call(self, kind, available):
        if not self.finishing and available <= TAIL[kind]:
            raise BudgetExhausted('candidate completion tail reserved before primary')

    def primary_passed(self):
        self.finishing = True
        self.trace('completion.finish', {'company': self.company})

    def close(self):
        ACTIVE.reset(self.token)
        self.trace('completion.release', {'company': self.company})


def primary_passed():
    lease = ACTIVE.get()
    if lease:
        lease.primary_passed()
