"""Classify provider error envelopes before interpreting completion/usage."""

def provider_error(response,http_status=None):
    if isinstance(response,dict):data=response
    elif hasattr(response,'model_dump'):data=response.model_dump()
    else:
        data=dict(getattr(response,'model_extra',None) or {})
        if hasattr(response,'error'):data['error']=response.error
    if not isinstance(data,dict) or 'error' not in data:return None
    error=data['error'];error=error if isinstance(error,dict) else {}
    metadata=error.get('metadata') or {};metadata=metadata if isinstance(metadata,dict) else {}
    code=error.get('code');kind=metadata.get('error_type') or error.get('type') or 'provider_error'
    try:numeric=int(code)
    except (TypeError,ValueError):numeric=None
    if numeric is None and isinstance(http_status,int) and http_status>=400:numeric=http_status
    retryable=numeric==429 or (numeric is not None and 500<=numeric<=599) or 'rate_limit' in str(code).lower() or 'rate_limit' in str(kind).lower() or 'rate-limit' in str(error.get('message','')).lower()
    # Do not trace arbitrary provider messages, metadata, request IDs or keys.
    return {'code':numeric,'error_type':str(kind)[:100],'retryable':retryable}


def exception_provider_error(exc):
    body=getattr(exc,'body',None)
    found=provider_error(body,getattr(exc,'status_code',None))
    if found is not None:return found
    response=getattr(exc,'response',None)
    if response is not None:
        try:return provider_error(response.json(),getattr(response,'status_code',None))
        except Exception:pass
    return None


def is_transport_timeout(exc):
    # SDK wraps socket/HTTP timeouts; shim failures can surface as a connection
    # error. Neither is a completed response with missing usage.
    import asyncio
    import httpx
    from openai import APITimeoutError, APIConnectionError
    return isinstance(exc,(asyncio.TimeoutError,TimeoutError,httpx.TimeoutException,APITimeoutError,APIConnectionError))
