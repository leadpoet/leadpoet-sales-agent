"""Profile sizes are hints; explicit page evidence remains independent."""
import math,re
from collections import Counter
from agent import v18_fit
from agent.v92_common import domain


def bounds(band):
    text=str(band).replace(',','').strip()
    if text.endswith('+'):return float(text[:-1]),math.inf
    parts=re.split(r'\s*[-–]\s*',text)
    if len(parts)==2:return float(parts[0]),float(parts[1])
    number=float(text);return number,number


def count(raw):
    if raw is None or isinstance(raw,bool):return None
    try:
        lo,hi=bounds(raw);value=lo if math.isinf(hi) else hi
        return value if math.isfinite(value) and value>=0 else None
    except (ValueError,TypeError):return None


def key(cand):return domain(cand.get('domain')) or str(cand.get('company_name','')).lower()


def register(run,cand,raw):
    if not hasattr(run,'v20r4_profiles'):run.v20r4_profiles={}
    run.v20r4_profiles[key(cand)]=raw


def inspect(run,cand,raw,allowed,trace,*,profile=False):
    n=count(raw);reason='numeric_hint'
    repeated=Counter(count(v) for v in getattr(run,'v20r4_profiles',{}).values())
    unknown=n is None or (profile and (n in (0,1,10) or repeated[n]>=3))
    if unknown:reason='sentinel_or_missing' if n is None or n in (0,1,10) else 'repeated_across_profiles'
    limits=[bounds(b) for b in allowed]
    conflict=bool(not unknown and limits and (n<.5*min(lo for lo,hi in limits) or n>2*max(hi for lo,hi in limits)))
    if conflict:reason='clearly_outside_margin'
    band=v18_fit.bucket(raw)
    if not conflict and band and band not in allowed:
        if not hasattr(run,'v20r4_margin_saved'):run.v20r4_margin_saved=set()
        run.v20r4_margin_saved.add(key(cand))
    trace('headcount.hint',{'company':cand.get('company_name'),'domain':key(cand),'raw_value':raw,
        'profile_count':raw if profile else None,'normalized_count':n,'bands':list(allowed),
        'source':'profile' if profile else 'cached_rows','decision':'conflict' if conflict else 'continue',
        'unknown':unknown,'reason':reason,'same_value_profiles':repeated[n] if profile and n is not None else 0})
    return {'count':None if unknown else n,'conflict':conflict,'unknown':unknown}


def claim(wanted,allowed,hint):
    n=hint.get('count')
    if n is not None:
        return min(allowed,key=lambda b:(max(bounds(b)[0]-n,0,n-bounds(b)[1]),bounds(b)[0]))
    return v18_fit.prior(wanted,allowed)


def saved(run):return len(getattr(run,'v20r4_margin_saved',set()))
