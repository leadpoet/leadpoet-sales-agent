"""One ICP's physical-call and conservative settled-plus-inflight money limits."""
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, ROUND_CEILING
import json, threading
from agent.deadline import BudgetExhausted

WORK=ContextVar('v15_work',default=('discovery','',False,False))
LANES={'discovery':6,'identity':6,'profile':5,'target':6,'reserve':6}
# USD micro-units; higher than the public catalog and at least the observed
# prompt/cache-write and completion rates. Unknown models fail closed.
PRICES={'google/gemini-3.1-flash-lite':(Decimal('0.25'),Decimal('1.50')),
        'openai/gpt-5.6-sol':(Decimal('6.25'),Decimal('30')),
        'openai/gpt-5.6-luna':(Decimal('0.20'),Decimal('1.20')),
        'deepseek/deepseek-v4-flash':(Decimal('0.089'),Decimal('0.177'))}
INTERNAL_REASONING={'google/gemini-3.1-flash-lite':Decimal('1.50')}
REQUEST_PRICES={}  # micro-USD per request; no fixed fee for current models.
DL_CEILINGS={'exa_search':30000,'exa_contents':10000,'firecrawl_scrape':10000,'generic_http_request':100000,
             'free_simple_company_search':0,'exa_company_search':30000}

@contextmanager
def work(lane,company='',primary=False,no_tools=False):
    token=WORK.set((lane,company,primary,no_tools))
    try:yield
    finally:WORK.reset(token)

def llm_ceiling(model,kwargs):
    if model not in PRICES:raise BudgetExhausted('unpriced model')
    parameters={k:v for k,v in kwargs.items() if k not in ('timeout','extra_body')}
    parameters.update(kwargs.get('extra_body') or {})
    prompt,completion=PRICES[model]
    chars=16+len(json.dumps(parameters,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))
    return int((chars*prompt+int(kwargs['max_tokens'])*(completion+INTERNAL_REASONING.get(model,Decimal(0)))+REQUEST_PRICES.get(model,Decimal(0))).to_integral_value(rounding=ROUND_CEILING))

class Budget:
    def __init__(self,trace,shape=True):
        self.trace=trace;self.lock=threading.RLock();self.lanes={k:0 for k in LANES}
        self.shape=shape
        self.identity_calls={};self.settled={'deepline':0,'openrouter':0,'scrapingdog':0};self.pending={};self.serial=0
        self.optional_calls=0;self.optional_ready=False
        self.peak=0;self.refusals=0;self.overrun=False
        self.salvage_calls=0

    def _reserve(self,provider,amount):
        combined=sum(self.settled.values())+sum(v[1] for v in self.pending.values())
        own=self.settled[provider]+sum(v[1] for v in self.pending.values() if v[0]==provider)
        if self.overrun or combined+amount>225000 or (provider=='openrouter' and own+amount>200000):
            self.refusals+=1
            self.trace('money.refused',{'provider':provider,'reserve_microusd':amount,'combined_microusd':combined})
            raise BudgetExhausted('per-ICP money reserve limit')
        self.serial+=1;self.pending[self.serial]=(provider,amount)
        self.peak=max(self.peak,combined+amount)
        self.trace('money.reserve',{'ticket':self.serial,'provider':provider,'microusd':amount})
        return self.serial

    def claim_dl(self,operation):
        with self.lock:
            lane,company,primary,no_tools=WORK.get()
            used=sum(self.lanes.values())
            if no_tools:raise BudgetExhausted('salvage has no provider calls')
            if operation not in DL_CEILINGS:raise BudgetExhausted('unpriced tool')
            if used>=29 or (self.shape and 29-used<=6 and not primary):raise BudgetExhausted('six completion calls reserved')
            if lane=='identity' and self.identity_calls.get(company,0)>=1:
                raise BudgetExhausted('one physical homepage call per candidate')
            bucket=lane
            if self.shape and self.lanes.get(bucket,0)>=LANES.get(bucket,0):
                if not primary or self.lanes['reserve']>=6:raise BudgetExhausted('purpose quota exhausted')
                bucket='reserve'
            ticket=self._reserve('deepline',DL_CEILINGS[operation])
            self.lanes[bucket]+=1
            if lane=='identity':self.identity_calls[company]=self.identity_calls.get(company,0)+1
            self.trace('budget.claim',{'lane':lane,'charged_lane':bucket,'company':company,'primary':primary,'operation':operation})
            return ticket

    def reserve_llm(self,model,kwargs):
        with self.lock:
            salvage=WORK.get()[3]
            if salvage and self.salvage_calls>=2:raise BudgetExhausted('two salvage model calls used')
            ticket=self._reserve('openrouter',llm_ceiling(model,kwargs))
            if salvage:self.salvage_calls+=1
            return ticket

    def available_llm(self):
        with self.lock:
            if self.overrun:return 0
            pending=sum(v[1] for v in self.pending.values())
            own_pending=sum(v[1] for v in self.pending.values() if v[0]=='openrouter')
            return max(0,min(225000-sum(self.settled.values())-pending,
                             200000-self.settled['openrouter']-own_pending))

    def settle(self,ticket,actual=None):
        with self.lock:
            provider,reserved=self.pending.pop(ticket)
            try:
                value=Decimal(str(actual))
                if isinstance(actual,bool) or not value.is_finite() or value<0:raise ValueError('invalid cost')
                charge=int((value*1000000).to_integral_value(rounding=ROUND_CEILING))
            except Exception:
                actual=None;charge=reserved
            self.settled[provider]+=charge
            # A price/envelope surprise is never hidden; stop all later paid calls.
            if charge>reserved or sum(self.settled.values())>225000 or self.settled['openrouter']>200000:
                self.overrun=True
            self.trace('money.settle',{'ticket':ticket,'provider':provider,'microusd':charge,
                                       'reported':actual is not None,'overrun':self.overrun})

    def claim_optional(self,operation,probe=False):
        with self.lock:
            lane,company,primary,no_tools=WORK.get()
            if no_tools or self.optional_calls>=30:raise BudgetExhausted('optional provider quota')
            if not self.optional_ready and not (probe and self.optional_calls==0):raise BudgetExhausted('optional provider unconfirmed')
            if lane=='identity' and self.identity_calls.get(company,0)>=1:raise BudgetExhausted('one physical homepage call per candidate')
            ticket=self._reserve('scrapingdog',10000)
            self.optional_calls+=1
            if lane=='identity':self.identity_calls[company]=self.identity_calls.get(company,0)+1
            return ticket

    def settle_dl(self,ticket,result):
        billing=(result or {}).get('billing') if isinstance(result,dict) else None
        from agent.v12_llm import enabled
        if enabled('V20R10_COST'):
            from agent.v20r10_cost import valid
            credits=valid(billing.get('credits_charged')) if isinstance(billing,dict) else None
            self.r10_credits=getattr(self,'r10_credits',Decimal(0))+(credits or Decimal(0))
            if credits is None:self.r10_unknown_dl=getattr(self,'r10_unknown_dl',0)+1
            return self.settle(ticket,float(credits*Decimal('.10')) if credits is not None else None)
        value=billing.get('cost_usd') if isinstance(billing,dict) else None
        if not isinstance(value,(float,int)) or isinstance(value,bool) or value<0:value=None
        self.settle(ticket,value)

    def snapshot(self):
        with self.lock:return {'settled_microusd':dict(self.settled),'inflight_microusd':sum(v[1] for v in self.pending.values()),
                               'peak_reserved_microusd':self.peak,'lanes':dict(self.lanes),'refusals':self.refusals,
                               'overrun':self.overrun,'openrouter_cap_microusd':200000,'combined_cap_microusd':225000,
                               'optional_provider_calls':self.optional_calls,
                               'price_basis':'conservative observed ceiling; stop on unexpected higher billed price'}
