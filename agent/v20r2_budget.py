"""Expected OpenRouter pacing with a separate transient maximum guard."""
from collections import Counter
from decimal import Decimal, ROUND_CEILING
from agent import v18_budget, v15_budget, v16_budget
from agent.deadline import BudgetExhausted

TARGET=225000
TRANSIENT=450000


def max_cost(model,kwargs):
    # Same canonical-JSON character bound and overhead as the broker. Prices
    # are local conservative prompt/completion ceilings, never billed costs.
    return v15_budget.llm_ceiling(model,kwargs)


def charge_microusd(value):
    try:
        d=Decimal(str(value))
        if isinstance(value,bool) or not d.is_finite() or d<0:raise ValueError('invalid charge')
        return int((d*1000000).to_integral_value(rounding=ROUND_CEILING))
    except Exception:return None


class Expected:
    """One ICP; fixed pending expectations, ratio learned only at settlement."""
    def __init__(self):
        self.settled=0;self.completed_max=0;self.pending={};self.overrun=False
        self.refusals=Counter();self.phase_cost=Counter();self.unreported=0
        self.transport_timeouts=0;self.timeout_reserved=0;self.provider_errors=0;self.provider_error_reserved=0;self.error_codes=Counter();self.error_types=Counter()

    @property
    def ratio(self):
        if not self.completed_max:return Decimal('.35')
        return max(Decimal('.20'),min(Decimal('1'),Decimal(self.settled)/self.completed_max))

    def expected(self,maximum):
        return int((Decimal(maximum)*self.ratio).to_integral_value(rounding=ROUND_CEILING))

    def reason(self,maximum):
        if self.overrun:return 'overrun'
        if self.settled>=TARGET:return 'settled_stop'
        if self.settled+maximum>TRANSIENT:return 'transient_max'
        if self.settled+sum(v[1] for v in self.pending.values())+self.expected(maximum)>TARGET:return 'expected_target'
        return ''

    def reserve(self,ticket,maximum,phase):
        reason=self.reason(maximum)
        if reason:
            self.refusals[reason]+=1
            raise BudgetExhausted(reason)
        self.pending[ticket]=(maximum,self.expected(maximum),phase)

    def settle(self,ticket,actual):
        maximum,expected,phase=self.pending.pop(ticket)
        charge=charge_microusd(actual)
        if charge is None:charge=maximum;self.unreported+=1;self.overrun=True
        self.settled+=charge;self.completed_max+=maximum;self.phase_cost[phase]+=charge
        self.overrun |= charge>maximum or self.settled>TARGET
        return charge

    def settle_timeout(self,ticket):
        maximum,expected,phase=self.pending.pop(ticket)
        self.transport_timeouts+=1;self.timeout_reserved+=maximum
        self.settled+=maximum;self.completed_max+=maximum;self.phase_cost[phase]+=maximum
        self.overrun |= self.settled>TARGET
        return maximum

    def settle_provider_error(self,ticket,error):
        maximum,expected,phase=self.pending.pop(ticket)
        self.provider_errors+=1;self.provider_error_reserved+=maximum
        self.error_codes[str(error.get('code'))]+=1;self.error_types[error.get('error_type','provider_error')]+=1
        # No completed tokens were purchased. Do not bias the learned ratio
        # with a failed request; validator reservation debit is informational.
        return 0

    def snapshot(self):
        return {'transport_timeout':self.transport_timeouts,'timeout_reserved_microusd':self.timeout_reserved,'provider_error':self.provider_errors,'provider_error_reserved_microusd':self.provider_error_reserved,
                'provider_errors_by_code':dict(self.error_codes),'provider_errors_by_type':dict(self.error_types),
                'expected_ratio':float(self.ratio),'refusals_by_rule':dict(self.refusals),
                'pending_expected_microusd':sum(v[1] for v in self.pending.values()),
                'completed_reserved_microusd':self.completed_max,
                'extraction_cost_microusd':self.phase_cost['extract'],
                'phase_a_cost_microusd':self.phase_cost['phase_a'],
                'other_openrouter_cost_microusd':sum(v for k,v in self.phase_cost.items() if k not in ('extract','phase_a')),
                'settled_target_microusd':TARGET,'transient_cap_microusd':TRANSIENT,
                'target_reached':self.settled>=TARGET,'overrun':self.overrun,
                'unreported_openrouter_calls':self.unreported}


class Budget(v18_budget.Budget):
    def __init__(self,*args,expected=True,**kwargs):
        super().__init__(*args,**kwargs);self.expected_mode=expected;self.expected_meter=Expected()

    def prediction_fits(self,model,kwargs):
        if not self.expected_mode:return super().prediction_fits(model,kwargs)
        with self.lock:return not self.expected_meter.reason(max_cost(model,kwargs))

    def note_prediction_refusal(self,model,kwargs):
        # Projection probes do not consume quota. Count only a final refusal,
        # when even the smallest complete cache projection cannot be admitted.
        with self.lock:
            maximum=max_cost(model,kwargs);reason=self.expected_meter.reason(maximum)
            if reason:
                self.refusals+=1;self.expected_meter.refusals[reason]+=1
                self.trace('money.refused',{'provider':'openrouter','rule':reason,
                    'new_max_microusd':maximum,'projection_exhausted':True,**self.expected_meter.snapshot()})

    def reserve_llm(self,model,kwargs):
        if not self.expected_mode:return super().reserve_llm(model,kwargs)
        with self.lock:
            lane=v15_budget.WORK.get();phase='phase_a' if lane[0]=='phase_a' else v16_budget.PHASE.get()[0]
            salvage=lane[3] and lane[0]!='phase_a'
            if salvage and self.salvage_calls>=2:raise BudgetExhausted('two salvage model calls used')
            maximum=max_cost(model,kwargs);ticket=self.serial+1
            try:self.expected_meter.reserve(ticket,maximum,phase)
            except BudgetExhausted as exc:
                self.refusals+=1
                self.trace('money.refused',{'provider':'openrouter','rule':str(exc),'new_max_microusd':maximum,**self.expected_meter.snapshot()})
                raise
            self.serial=ticket;self.pending[ticket]=('openrouter',maximum)
            self.peak=max(self.peak,self.expected_meter.settled+sum(v[0] for v in self.expected_meter.pending.values()))
            if phase=='extract':self.first_extraction_started=True
            if salvage:self.salvage_calls+=1
            self.trace('money.reserve',{'ticket':ticket,'provider':'openrouter','microusd':maximum,'phase':phase,
                                       'expected_microusd':self.expected_meter.pending[ticket][1],'expected_ratio':float(self.expected_meter.ratio)})
            return ticket

    def settle(self,ticket,actual=None):
        if not self.expected_mode or self.pending[ticket][0]!='openrouter':return super().settle(ticket,actual)
        with self.lock:
            self.pending.pop(ticket)
            charge=self.expected_meter.settle(ticket,actual)
            self.settled['openrouter']=self.expected_meter.settled;self.overrun=self.expected_meter.overrun
            self.trace('money.settle',{'ticket':ticket,'provider':'openrouter','microusd':charge,'reported':charge_microusd(actual) is not None,
                                      **self.expected_meter.snapshot()})

    def settle_timeout(self,ticket):
        if not self.expected_mode:
            maximum=self.pending[ticket][1]
            return super().settle(ticket,maximum/1000000)
        with self.lock:
            self.pending.pop(ticket);charge=self.expected_meter.settle_timeout(ticket)
            self.settled['openrouter']=self.expected_meter.settled;self.overrun=self.expected_meter.overrun
            self.trace('money.settle',{'ticket':ticket,'provider':'openrouter','microusd':charge,
                'outcome':'transport_timeout','reported':False,'basis':'full_reservation',**self.expected_meter.snapshot()})

    def settle_provider_error(self,ticket,error):
        if not self.expected_mode:return super().settle(ticket,0)
        with self.lock:
            self.pending.pop(ticket);self.expected_meter.settle_provider_error(ticket,error)
            self.trace('money.settle',{'ticket':ticket,'provider':'openrouter','microusd':0,
                'outcome':'provider_error','reported':False,'provider_error_code':error.get('code'),
                'provider_error_type':error.get('error_type'),**self.expected_meter.snapshot()})

    def snapshot(self):
        result=super().snapshot()
        if self.expected_mode:
            result.pop('openrouter_cap_microusd',None);result.pop('combined_cap_microusd',None)
            result.pop('request_sizing_microusd',None)
            result.update(self.expected_meter.snapshot(),basis='settled_plus_pending_expected_plus_new_expected',
                          prediction='running settled/completed maximum ratio, clamped [0.20,1.00]')
        return result
