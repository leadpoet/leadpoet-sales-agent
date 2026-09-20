"""Predictive OpenRouter admission; separately bounded page/lookup calls."""
import json
from decimal import Decimal,ROUND_CEILING
from agent import v17_budget,v16_budget,v15_budget
from agent.deadline import BudgetExhausted

CAP=250000
STAGE_LOOKUP_CAP=12
STAGE_CONFIRM_RESERVE=10

def predicted_cost(model,kwargs):
    if model not in v15_budget.PRICES:raise BudgetExhausted('unpriced model')
    params={k:v for k,v in kwargs.items() if k not in ('timeout','extra_body')}
    params.update(kwargs.get('extra_body') or {})
    # Byte-level upper bound, including message framing; do not assume four
    # characters per token, or bill reasoning tokens as free completions.
    size=128+len(json.dumps(params,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8'))
    prompt,completion=v15_budget.PRICES[model]
    return int((size*prompt+int(kwargs['max_tokens'])*(completion+v15_budget.INTERNAL_REASONING.get(model,Decimal(0)))+v15_budget.REQUEST_PRICES.get(model,Decimal(0))).to_integral_value(rounding=ROUND_CEILING))

class Budget(v17_budget.Budget):
    def __init__(self,*args,predict=True,redirect=True,**kwargs):
        super().__init__(*args,**kwargs);self.predict=predict;self.redirect=redirect
        self.redirect_homes={};self.redirect_searches=set();self.stage_lookups=set()
        self.size_searches={};self.linkedin_size_checks=set()

    def _reserve(self,provider,amount):
        if not self.predict:return super()._reserve(provider,amount)
        exposure=self.settled['openrouter']+sum(n for p,n in self.pending.values() if p=='openrouter')
        if provider=='openrouter' and (self.overrun or exposure+amount>CAP):
            self.refusals+=1
            self.trace('money.refused',{'provider':provider,'basis':'openrouter_predictive',
                'settled_and_inflight_microusd':exposure,'new_reservation_microusd':amount,'cap_microusd':CAP})
            raise BudgetExhausted('predictive OpenRouter cap')
        self.serial+=1;self.pending[self.serial]=(provider,amount)
        self.peak=max(self.peak,exposure+(amount if provider=='openrouter' else 0))
        self.trace('money.reserve',{'ticket':self.serial,'provider':provider,'microusd':amount,
            'exposure_before_new_call_microusd':exposure,'basis':'openrouter_predictive',
            'first_extraction_exception':False,'cap_microusd':CAP})
        return self.serial

    def reserve_llm(self,model,kwargs):
        if not self.predict:return super().reserve_llm(model,kwargs)
        with self.lock:
            salvage=v15_budget.WORK.get()[3] and v15_budget.WORK.get()[0]!='phase_a'
            if salvage and self.salvage_calls>=2:raise BudgetExhausted('two salvage model calls used')
            # Preserve the independent small wire-request sizing rule.
            if v16_budget.request_ceiling(model,kwargs)>80000:raise BudgetExhausted('request sizing exceeded')
            ticket=self._reserve('openrouter',predicted_cost(model,kwargs))
            if v16_budget.PHASE.get()[0]=='extract':self.first_extraction_started=True
            if salvage:self.salvage_calls+=1
            return ticket

    def prediction_fits(self,model,kwargs):
        with self.lock:
            pending=sum(n for p,n in self.pending.values() if p=='openrouter')
            return (not self.overrun and self.settled['openrouter']+pending+predicted_cost(model,kwargs)<=CAP-1000
                    and v16_budget.request_ceiling(model,kwargs)<=79000)

    def settle(self,ticket,actual=None):
        with self.lock:
            provider,reserved=self.pending[ticket]
            super().settle(ticket,actual)
            if self.predict and provider=='openrouter':
                if self.settled['openrouter']>CAP:self.overrun=True
                if actual is not None and Decimal(str(actual))*1000000>reserved:self.overrun=True

    def claim_dl(self,operation):
        from agent.v12_llm import enabled
        if enabled('V20R7_ALLOCATION'):
            from agent.v20r7_allocation import claim
            return claim(self,operation)
        if not enabled('V20R6_EVENT_LOOKUP'):return self._claim_dl(operation)
        lane,company,primary,no_tools=v15_budget.WORK.get()
        with self.lock:
            used=sum(self.lanes.values())
            remaining=getattr(self,'r6_confirmation_remaining',0)
            identity=lane in ('v18_identity','v17_identity','identity','salvage_identity')
            if lane=='v20r6_event':
                events=getattr(self,'r6_event_lookups',set())
                if no_tools or not primary or operation!='exa_search':raise BudgetExhausted('event lookup requires affirmed stage')
                if len(events)>=5 or company in events:raise BudgetExhausted('five distinct event lookups maximum')
                if used>=24:raise BudgetExhausted('five identity calls reserved')
                ticket=self._reserve('deepline',v15_budget.DL_CEILINGS[operation])
                events.add(company);self.r6_event_lookups=events;self.lanes['reserve']+=1
                self.trace('budget.claim',{'lane':lane,'charged_lane':'reserve','company':company,'stage_affirmed':True,'operation':operation})
                return ticket
            if identity and getattr(self,'r6_identity_calls',0)>=5:raise BudgetExhausted('five identity calls maximum')
            if lane in ('profile','v21_size','v21_linkedin') or identity:
                if used>=29-remaining:raise BudgetExhausted('remaining identities reserved')
            ticket=self._claim_dl(operation)
            if identity:self.r6_identity_calls=getattr(self,'r6_identity_calls',0)+1
            return ticket

    def _claim_dl(self,operation):
        lane,company,primary,no_tools=v15_budget.WORK.get()
        if lane in ('v21_size','v21_linkedin'):
            from agent.v21_size import MAX_SEARCHES_PER_COMPANY,MAX_SEARCHES_PER_ICP
            with self.lock:
                if no_tools or not primary:raise BudgetExhausted('size proof requires a verified primary')
                if sum(self.lanes.values())>=29:raise BudgetExhausted('29 Deepline calls exhausted')
                if lane=='v21_size':
                    if operation!='exa_search' or self.size_searches.get(company,0)>=MAX_SEARCHES_PER_COMPANY or sum(self.size_searches.values())>=MAX_SEARCHES_PER_ICP:
                        raise BudgetExhausted('headcount search allowance exhausted')
                elif operation!='exa_contents' or company in self.linkedin_size_checks:
                    raise BudgetExhausted('one LinkedIn size check per company')
                ticket=self._reserve('deepline',v15_budget.DL_CEILINGS[operation])
                if lane=='v21_size':self.size_searches[company]=self.size_searches.get(company,0)+1
                else:self.linkedin_size_checks.add(company)
                self.lanes['reserve']+=1
                self.trace('budget.claim',{'lane':lane,'charged_lane':'reserve','company':company,'primary':True,'operation':operation})
                return ticket
        if lane not in ('v18_identity','v18_stage'):return super().claim_dl(operation)
        with self.lock:
            if no_tools or not primary:raise BudgetExhausted('lookup requires verified primary')
            if sum(self.lanes.values())>=29:raise BudgetExhausted('29 Deepline calls exhausted')
            if lane=='v18_stage':
                if sum(self.lanes.values())>=29-STAGE_CONFIRM_RESERVE:
                    raise BudgetExhausted('ten confirmation calls reserved')
                if operation!='exa_search' or company in self.stage_lookups or len(self.stage_lookups)>=STAGE_LOOKUP_CAP:
                    raise BudgetExhausted('twelve distinct stage lookups maximum')
                self.stage_lookups.add(company)
            else:
                if company not in self.identity_candidates and len(self.identity_candidates)>=8:
                    raise BudgetExhausted('eight identity candidates maximum')
                if operation=='exa_company_search':
                    if company in self.redirect_searches:raise BudgetExhausted('one identity search maximum')
                    self.redirect_searches.add(company)
                elif operation in ('firecrawl_scrape','generic_http_request'):
                    if self.redirect_homes.get(company,0)>=2:raise BudgetExhausted('two homepage fetches maximum')
                    self.redirect_homes[company]=self.redirect_homes.get(company,0)+1
                else:raise BudgetExhausted('identity operation not allowed')
                self.identity_candidates.add(company)
            ticket=self._reserve('deepline',v15_budget.DL_CEILINGS[operation]);self.lanes['reserve']+=1
            self.trace('budget.claim',{'lane':lane,'charged_lane':'reserve','company':company,'primary':True,'operation':operation})
            return ticket

    def snapshot(self):
        result=super().snapshot()
        if self.predict:result.update(basis='openrouter_settled_plus_inflight_plus_new_reservation',
          settled_target_microusd=CAP,target_reached=self.settled['openrouter']>=CAP,
          prediction='UTF-8 byte upper bound plus maximum completion at conservative rates',overrun=self.overrun)
        result.update(size_evidence_search_calls=sum(self.size_searches.values()),linkedin_size_calls=len(self.linkedin_size_checks),stage_lookups_spent=len(self.stage_lookups),redirect_homepage_calls=sum(self.redirect_homes.values()))
        from agent.v12_llm import enabled
        if enabled('V20R6_EVENT_LOOKUP'):result.update(category_event_lookups_spent=len(getattr(self,'r6_event_lookups',set())),r6_identity_calls=getattr(self,'r6_identity_calls',0))
        if enabled('V20R7_ALLOCATION'):result.update(r7_event_calls=len(getattr(self,'r7_events',set())),r7_stage_calls=len(getattr(self,'r7_stages',set())),r7_identity_calls=sum(getattr(self,'r7_identities',{}).values()),r7_proof_calls=getattr(self,'r7_proof_calls',0))
        return result
