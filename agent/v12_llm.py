"""Bounded structured-output policy; an incomplete answer is not a verdict."""
import asyncio
import json
import os
import re


def enabled(name):
    # Contact fields require the input's contact_policy, irrespective of this
    # retired opt-in. Intent Details selects the v5 wire format independently.
    if name == 'V35_CONTACTS_ALWAYS':
        return os.environ.get('AGENT_RULE_' + name, '0').lower() not in ('0', 'false', 'off', 'no')
    default = '0' if name.startswith('V32_') or (name.startswith('V33_') and name != 'V33_BAND_GUARD') else '1'
    return os.environ.get('AGENT_RULE_' + name, default).lower() not in ('0', 'false', 'off', 'no')


PHASE_A_MODELS=('google/gemini-3.1-flash-lite','openai/gpt-5.6-luna')

def effective_model(run):
    from agent.v15_budget import WORK
    if WORK.get()[0]=='phase_a' and enabled('V20R2_CHEAP_PHASE_A'):
        return getattr(run,'v20r2_phase_a_model',PHASE_A_MODELS[0])
    from agent.v16_budget import PHASE
    if PHASE.get()[0]=='extract' and enabled('V20R10_COST'):
        return getattr(run,'v20r10_extract_model',PHASE_A_MODELS[0])
    return getattr(run,'model','openai/gpt-5.6-sol')


class LLMTruncated(RuntimeError):
    """No complete JSON verdict after the one permitted recovery attempt."""


def parse_json(content):
    text = str(content or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-z]*\s*', '', text)
        if text.endswith('```'):
            text = text[:-3].strip()
    try:
        value = json.loads(text)
    except ValueError:
        return None
    # All current callers request a JSON object, never prose or a scalar.
    return value if isinstance(value, dict) else None


def ceiling(requested):
    # v11 maxima: extraction 1030, stage extraction 667, pick 70,
    # primary verdict 900 (clipped), secondary verdict 244. Unobserved
    # shapes receive twice their previous ceiling, rather than a guessed length.
    return min(4096, {900: 2500, 1800: 2500}.get(requested, requested * 2))


async def ask(run, system, user, *, max_tokens, planning, trace, parse_legacy,
              llm_seconds, budget_error):
    model = effective_model(run)
    reasoning = enabled('V12_REASONING')
    truncation = enabled('V12_TRUNCATION')
    meter = getattr(run, 'v15_budget', None)
    initial = min(2500, max_tokens) if meter else (ceiling(max_tokens) if reasoning else min(4096, max_tokens))
    incomplete = False
    reason = ''
    attempt=0;provider_retries=0
    from agent.v15_budget import WORK
    from agent.v20r2_errors import provider_error,exception_provider_error
    phase_a=WORK.get()[0]=='phase_a' and enabled('V20R2_CHEAP_PHASE_A')
    from agent.v16_budget import PHASE
    extraction=PHASE.get()[0]=='extract' and enabled('V20R10_COST') and not phase_a
    cheap=phase_a or extraction
    prefix='phase_a' if phase_a else 'extract'
    model_attr='v20r2_phase_a_model' if phase_a else 'v20r10_extract_model'
    exhausted_attr='v20r2_phase_a_exhausted' if phase_a else 'v20r10_extract_exhausted'
    if cheap and getattr(run,exhausted_attr,False):return None
    while attempt < (2 if truncation else 1):
        model=effective_model(run)
        if run.remaining() <= 0 and not incomplete:
            raise budget_error('planning deadline exhausted')
        if run.remaining() < 8 or run.llm_requests >= getattr(run, 'llm_budget', 60):
            if incomplete:
                reason += ':retry_budget_or_time'
                break
            return None
        # No await between quota check and increment: physical retries count.
        tokens = 4096 if attempt else initial
        effort = 'low' if model.startswith('openai/') and (attempt or (reasoning and not planning)) else None
        timeout = min(llm_seconds, run.remaining())
        if cheap:
            timeout=min(timeout,30,run.remaining()-20)
            if timeout<1:
                setattr(run,exhausted_attr,True)
                trace(prefix+'.model_exhausted',{'model':model,'reason':'phase_b_time_reserve'})
                return None
        kwargs = dict(model=model,
                      messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                      max_tokens=tokens, temperature=0.1,
                      extra_body={'usage': {'include': True}}, timeout=timeout)
        if effort:
            kwargs['reasoning_effort'] = effort
        try:
            ticket = meter.reserve_llm(model, kwargs) if meter else None
        except budget_error:
            if incomplete:
                reason += ':retry_money'
                break
            raise
        run.llm_requests += 1
        call_id = run.llm_requests
        trace('llm.request', {'call_id': call_id, 'attempt': attempt + 1,
                             'model': model, 'max_tokens': tokens, 'timeout_seconds':timeout,
                             'reasoning_effort': effort, 'planning': planning})
        response_error=None;transport_timeout=False
        try:
            resp = await asyncio.wait_for(run.llm.chat.completions.create(**kwargs), timeout=timeout)
            response_error=provider_error(resp)
        except asyncio.CancelledError:
            if meter:meter.settle(ticket)
            if incomplete:
                run.llm_truncated = getattr(run, 'llm_truncated', 0) + 1
                trace('llm.truncated', {'why': 'llm_truncated', 'detail': 'retry_cancelled_after_incomplete'})
            raise
        except Exception as exc:
            response_error=exception_provider_error(exc)
            from agent.v20r2_errors import is_transport_timeout
            transport_timeout=cheap and is_transport_timeout(exc)
            if transport_timeout:
                response_error={'code':None,'error_type':'transport_timeout','retryable':True}
            if response_error is None:
                if meter:meter.settle(ticket)
                trace('llm.error', {'call_id':call_id,'error':f'{type(exc).__name__}: {str(exc)[:200]}'})
                if incomplete:
                    reason += ':retry_error'
                    break
                return None
        if response_error is not None:
            if meter:
                if transport_timeout and hasattr(meter,'settle_timeout'):meter.settle_timeout(ticket)
                elif hasattr(meter,'settle_provider_error'):meter.settle_provider_error(ticket,response_error)
                else:meter.settle(ticket,0)
            trace('llm.timeout' if transport_timeout else 'llm.provider_error',{'call_id':call_id,'model':model,**response_error})
            if cheap:
                if provider_retries==0:
                    if run.remaining()<23 or run.llm_requests>=getattr(run,'llm_budget',60):
                        setattr(run,exhausted_attr,True)
                        trace(prefix+'.model_exhausted',{'model':model,'reason':'retry_budget_or_time'})
                        return None
                    trace(prefix+'.model_retry',{'model':model,'backoff_seconds':2,'retry':1,**response_error})
                    await asyncio.sleep(2)
                    provider_retries=1
                    continue
                index=PHASE_A_MODELS.index(model)
                if index+1<len(PHASE_A_MODELS):
                    setattr(run,model_attr,PHASE_A_MODELS[index+1])
                    trace(prefix+'.model_fallback',{'from':model,'to':getattr(run,model_attr),'rest_of_phase_a':phase_a,'rest_of_extraction':extraction,**response_error})
                    provider_retries=0
                    continue
                setattr(run,exhausted_attr,True)
                trace(prefix+'.model_exhausted',{'model':model,'reason':'chain_exhausted',**response_error})
            # A provider error is not truncated JSON and never a fit verdict.
            return None
        usage = getattr(resp, 'usage', None)
        if meter:
            reported = (getattr(usage, 'model_extra', None) or {}).get('cost') if usage else None
            meter.settle(ticket, reported if isinstance(reported,(int,float)) and reported>=0 else None)
        if usage is not None:
            run.input_tokens += int(getattr(usage, 'prompt_tokens', 0) or 0)
            run.output_tokens += int(getattr(usage, 'completion_tokens', 0) or 0)
            extra = getattr(usage, 'model_extra', None) or {}
            try:
                run.cost += float(extra.get('cost') or 0.0)
            except (TypeError, ValueError):
                pass
        choice = resp.choices[0] if resp.choices else None
        finish = getattr(choice, 'finish_reason', None)
        content = choice.message.content if choice else ''
        result = parse_json(content) if truncation else parse_legacy(content)
        bad = truncation and (finish == 'length' or result is None)
        trace('llm.result', {'call_id': call_id, 'system': system, 'user': user,
                             'max_tokens': tokens, 'reasoning_effort': effort,
                             'finish_reason': finish, 'attempt': attempt + 1,
                             'inconclusive': bad, 'result': None if bad else result})
        if not bad:
            if incomplete:
                run.llm_truncation_recovered = getattr(run, 'llm_truncation_recovered', 0) + 1
                trace('llm.recovered', {'call_id': call_id})
            return result
        incomplete = True
        reason = 'length' if finish == 'length' else 'json_parse'
        run.llm_incomplete_responses = getattr(run, 'llm_incomplete_responses', 0) + 1
        trace('llm.inconclusive', {'call_id': call_id, 'reason': reason, 'attempt': attempt + 1})
        attempt += 1
        if extraction and attempt >= (2 if truncation else 1) and model==PHASE_A_MODELS[0]:
            setattr(run,model_attr,PHASE_A_MODELS[1])
            trace('extract.model_fallback',{'from':model,'to':PHASE_A_MODELS[1],'reason':'incomplete_json_after_recovery','rest_of_extraction':True})
            attempt=0;provider_retries=0
    run.llm_truncated = getattr(run, 'llm_truncated', 0) + 1
    trace('llm.truncated', {'why': 'llm_truncated', 'detail': reason})
    raise LLMTruncated(reason)
