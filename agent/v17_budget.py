"""OpenRouter-only pacing; Deepline has a physical-call budget, not an OR bill."""
from decimal import Decimal, ROUND_CEILING
from agent import v15_budget as old, v16_budget as previous
from agent.deadline import BudgetExhausted

class Budget(previous.Budget):
    def __init__(self,*args,or_only=True,domains=True,**kwargs):
        super().__init__(*args,**kwargs);self.or_only=or_only;self.domains=domains
        self.identity_searches={};self.identity_candidates=set()

    def _reserve(self,provider,amount):
        if not self.or_only:return super()._reserve(provider,amount)
        exposure=self.settled['openrouter']+sum(v[1] for v in self.pending.values() if v[0]=='openrouter')
        first=provider=='openrouter' and previous.PHASE.get()==('extract',True) and not self.first_extraction_started
        if provider=='openrouter' and exposure>=220000 and not first:
            self.refusals+=1
            self.trace('money.refused',{'provider':provider,'basis':'openrouter_settled_plus_other_openrouter_inflight',
                       'exposure_microusd':exposure,'new_call_reservation_microusd':amount})
            raise BudgetExhausted('OpenRouter settled spend target reached')
        self.serial+=1;self.pending[self.serial]=(provider,amount)
        self.peak=max(self.peak,exposure+(amount if provider=='openrouter' else 0))
        self.trace('money.reserve',{'ticket':self.serial,'provider':provider,'microusd':amount,
                   'exposure_before_new_call_microusd':exposure,'first_extraction_exception':first})
        return self.serial

    def settle(self,ticket,actual=None):
        if not self.or_only:return super().settle(ticket,actual)
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
                'above_request_estimate':charge>reserved,'target_reached':self.settled['openrouter']>=220000,
                'target_provider':'openrouter'})

    def claim_dl(self,operation):
        if not self.or_only and not self.domains:return super().claim_dl(operation)
        with self.lock:
            lane,company,primary,no_tools=old.WORK.get()
            if no_tools:raise BudgetExhausted('salvage has no general provider calls')
            if operation not in old.DL_CEILINGS:raise BudgetExhausted('unpriced tool')
            if sum(self.lanes.values())>=29:raise BudgetExhausted('29 Deepline physical calls exhausted')
            if lane=='v17_identity':
                if not primary:raise BudgetExhausted('identity requires a verified cached primary')
                if company not in self.identity_candidates and len(self.identity_candidates)>=8:
                    raise BudgetExhausted('eight identity candidates exhausted')
                if operation not in ('exa_contents','exa_company_search'):raise BudgetExhausted('identity operation not allowed')
                used=self.identity_searches if operation=='exa_company_search' else self.identity_calls
                if used.get(company,0):raise BudgetExhausted('one homepage and one company search per candidate')
                ticket=self._reserve('deepline',old.DL_CEILINGS[operation])
                self.identity_candidates.add(company);used[company]=1;bucket='reserve'
            else:
                if lane in ('identity','salvage_identity') and self.identity_calls.get(company,0):
                    raise BudgetExhausted('one physical homepage call per candidate')
                ticket=self._reserve('deepline',old.DL_CEILINGS[operation]);bucket=lane
                if lane in ('identity','salvage_identity'):self.identity_calls[company]=1
            self.lanes[bucket]=self.lanes.get(bucket,0)+1
            self.trace('budget.claim',{'lane':lane,'charged_lane':bucket,'company':company,'primary':primary,'operation':operation})
            return ticket

    def snapshot(self):
        data=super().snapshot()
        if self.or_only:
            data.update(basis='openrouter_settled_plus_other_openrouter_inflight; exclude own reservation',
                        target_provider='openrouter',target_reached=self.settled['openrouter']>=220000,
                        deepline_money_cap=None,identity_candidates=len(self.identity_candidates))
        return data
