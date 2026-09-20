"""Report sourcing spend separately from unverified output-based allowances."""
from decimal import Decimal

def valid(value):
    try:
        number=Decimal(str(value))
        return number if not isinstance(value,bool) and number.is_finite() and number>=0 else None
    except Exception:return None

def discovery_stop(calls,candidates):
    return calls>=6 and len(candidates)>=20

def summary(run,returned):
    meter=run.v15_budget;s=meter.snapshot()
    credits=getattr(meter,'r10_credits',Decimal(0));unknown=getattr(meter,'r10_unknown_dl',0)
    settled=s['settled_microusd'];inflight=s.get('inflight_microusd',0)
    provider_error=s.get('provider_error_reserved_microusd',0)
    return {'deepline_credits_reported':float(credits),'deepline_usd_reported':float(credits*Decimal('.10')),
        'deepline_usd_settled_or_reserved':settled['deepline']/1e6,
        'openrouter_usd_settled':settled['openrouter']/1e6,'other_provider_usd':sum(v for k,v in settled.items() if k not in ('deepline','openrouter'))/1e6,
        'sourcing_usd_settled':sum(settled.values())/1e6,
        'sourcing_usd_upper_bound':(sum(settled.values())+inflight+provider_error)/1e6,
        'provider_error_reserved_usd':provider_error/1e6,'inflight_usd':inflight/1e6,
        'unknown_deepline_calls':unknown,'billing_resolved':not (unknown or inflight or provider_error or s.get('unreported_openrouter_calls',0) or s.get('transport_timeout',0)),
        'returned_companies':returned,'openrouter_target_usd':.05,
        'openrouter_target_met':settled['openrouter']<=50000}

def eligibility(spend,returned,completed_icps=1,*,billing_resolved=True):
    scale=20/max(1,completed_icps);projected=spend*scale;companies=returned*scale
    allowance=min(50,.5*companies)
    return {'projected_icps':20,'observed_icps':completed_icps,'returned_companies':returned,
        'projected_returned_companies':companies,'observed_spend_usd':spend,'projected_spend_usd':projected,
        'observed_returned_allowance_usd':min(50,.5*returned),'projected_returned_allowance_usd':allowance,
        'within_returned_based_estimate':projected<=allowance if billing_resolved else None,
        'billing_resolved':billing_resolved,'verified_companies':None,'actual_eligibility':None,
        'basis':'output-count estimate only; integrity qualification requires independent verified companies'}

async def discover_extract(queries,search,rows,extract,merge,trace):
    candidates=[];processed=set();extracted=False
    for index,query in enumerate(queries):
        if discovery_stop(index,candidates):
            trace('discovery.stop',{'reason':'six_calls_twenty_extracted','calls':index,'candidates':len(candidates)})
            break
        await search(query)
        if index==5:
            current=rows();candidates=await extract(current)
            processed={(r['url'],r.get('text','')) for r in current};extracted=True
    current=[r for r in rows() if (r['url'],r.get('text','')) not in processed]
    if current or not extracted:candidates=merge(candidates,await extract(current))
    return candidates
