"""Settled-spend pacing, separate from broker-style request-size estimates."""
from contextvars import ContextVar
from contextlib import contextmanager
from decimal import Decimal,ROUND_CEILING
import json
from agent import v15_budget as old
from agent.deadline import BudgetExhausted

PHASE=ContextVar('v16_phase',default=('other',False))
REQUEST_PRICES={'google/gemini-3.1-flash-lite':(Decimal('.25'),Decimal('1.50')),
                'openai/gpt-5.6-sol':(Decimal('2'),Decimal('10')),
                'openai/gpt-5.6-luna':(Decimal('0.20'),Decimal('1.20')),
                'deepseek/deepseek-v4-flash':(Decimal('0.089'),Decimal('0.177'))}

@contextmanager
def phase(name,first=False):
    token=PHASE.set((name,first))
    try:yield
    finally:PHASE.reset(token)

def request_ceiling(model,kwargs):
    if model not in REQUEST_PRICES:raise BudgetExhausted('unpriced request model')
    parameters={k:v for k,v in kwargs.items() if k not in ('timeout','extra_body')}
    parameters.update(kwargs.get('extra_body') or {})
    prompt,completion=REQUEST_PRICES[model]
    size=16+len(json.dumps(parameters,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))
    return int((size*prompt+int(kwargs['max_tokens'])*(completion+old.INTERNAL_REASONING.get(model,Decimal(0)))+old.REQUEST_PRICES.get(model,Decimal(0))).to_integral_value(rounding=ROUND_CEILING))

class Budget(old.Budget):
    def __init__(self,trace,shape=True,*,settled_mode=True,identity_mode=True):
        super().__init__(trace,shape);self.settled_mode=settled_mode;self.identity_mode=identity_mode
        self.first_extraction_started=False;self.price_surprises=0;self.unreported_cost_calls=0

    def _reserve(self,provider,amount):
        if not self.settled_mode:return super()._reserve(provider,amount)
        # Other in-flight calls count; this new call's own maximum is NOT spend.
        exposure=sum(self.settled.values())+sum(v[1] for v in self.pending.values())
        first=(provider=='openrouter' and PHASE.get()==('extract',True) and not self.first_extraction_started)
        if exposure>=220000 and not first:
            self.refusals+=1
            self.trace('money.refused',{'provider':provider,'basis':'settled_plus_other_inflight',
                       'exposure_microusd':exposure,'new_call_reservation_microusd':amount})
            raise BudgetExhausted('settled spend target reached')
        self.serial+=1;self.pending[self.serial]=(provider,amount)
        self.peak=max(self.peak,exposure+amount)
        self.trace('money.reserve',{'ticket':self.serial,'provider':provider,'microusd':amount,
                   'exposure_before_new_call_microusd':exposure,'first_extraction_exception':first})
        return self.serial

    def reserve_llm(self,model,kwargs):
        if not self.settled_mode:return super().reserve_llm(model,kwargs)
        with self.lock:
            salvage=old.WORK.get()[3]
            if salvage and self.salvage_calls>=2:raise BudgetExhausted('two salvage model calls used')
            # Sizing is done before ask, independently of admission. Legacy
            # $0.17-shaped requests remain admissible to the spend guard itself.
            ticket=self._reserve('openrouter',request_ceiling(model,kwargs))
            if PHASE.get()[0]=='extract':self.first_extraction_started=True
            if salvage:self.salvage_calls+=1
            return ticket

    def available_llm(self):
        return 80000 if self.settled_mode else super().available_llm()

    def settle(self,ticket,actual=None):
        if not self.settled_mode:return super().settle(ticket,actual)
        with self.lock:
            provider,reserved=self.pending.pop(ticket)
            try:
                value=Decimal(str(actual))
                if isinstance(actual,bool) or not value.is_finite() or value<0:raise ValueError('invalid cost')
                charge=int((value*1000000).to_integral_value(rounding=ROUND_CEILING))
            except Exception:
                actual=None;charge=reserved;self.unreported_cost_calls+=1
            self.settled[provider]+=charge
            if charge>reserved:self.price_surprises+=1
            self.trace('money.settle',{'ticket':ticket,'provider':provider,'microusd':charge,'reported':actual is not None,
                       'above_request_estimate':charge>reserved,'target_reached':sum(self.settled.values())>=220000})

    def claim_dl(self,operation):
        lane,company,primary,no_tools=old.WORK.get()
        if lane!='salvage_identity':return super().claim_dl(operation)
        with self.lock:
            if not self.identity_mode or not primary or no_tools:raise BudgetExhausted('salvage identity not authorized')
            if operation not in ('exa_contents','firecrawl_scrape'):raise BudgetExhausted('salvage has no discovery or profile calls')
            if sum(self.lanes.values())>=29 or self.lanes['reserve']>=6:raise BudgetExhausted('salvage reserve exhausted')
            if self.identity_calls.get(company,0)>=1:raise BudgetExhausted('one physical homepage call per candidate')
            ticket=self._reserve('deepline',old.DL_CEILINGS[operation])
            self.lanes['reserve']+=1;self.identity_calls[company]=self.identity_calls.get(company,0)+1
            self.trace('budget.claim',{'lane':lane,'charged_lane':'reserve','company':company,'primary':True,'operation':operation})
            return ticket

    def snapshot(self):
        data=super().snapshot()
        if self.settled_mode:
            data.pop('combined_cap_microusd',None);data.pop('openrouter_cap_microusd',None)
            data.update(settled_target_microusd=220000,request_sizing_microusd=80000,
                        basis='settled_plus_other_inflight; exclude own reservation',
                        first_extraction_started=self.first_extraction_started,price_surprises=self.price_surprises,
                        unreported_cost_calls=self.unreported_cost_calls,
                        target_reached=sum(self.settled.values())>=220000,
                        price_basis='broker-style catalog reservation; actual reported billing for spend')
        return data
