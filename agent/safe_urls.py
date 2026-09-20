"""Drop malformed URL inputs locally without aborting other candidates."""
import json,os,time
from urllib.parse import urlsplit as _urlsplit,urlparse as _urlparse,urljoin as _urljoin,urlunsplit as _urlunsplit

def invalid(value,error):
    path=os.environ.get('AGENT_TRACE_PATH')
    if not path:return
    # Never copy URL credentials, queries or fragments into telemetry.
    from re import sub
    safe=sub(r'(://)[^/@]+@',r'\1[credentials]@',str(value or '').split('?',1)[0].split('#',1)[0])[:240]
    try:
        with open(path,'a',encoding='utf-8') as out:
            out.write(json.dumps({'t':round(time.time(),1),'stage':'url.invalid',
                       'data':{'url':safe,'reason':type(error).__name__,'decision':'drop_url'}},ensure_ascii=True)+'\n')
    except OSError:pass

def _checked(value,parser,scheme='',allow_fragments=True):
    parsed=parser(value,scheme,allow_fragments)
    # These properties can raise lazily even after parsing succeeds.
    host=parsed.hostname;parsed.port
    if host:
        host.encode('idna')
        if any(c.isspace() for c in host):raise ValueError('Whitespace in host')
    return parsed

def urlsplit(value,scheme='',allow_fragments=True):
    try:return _checked(value,_urlsplit,scheme,allow_fragments)
    except (ValueError,UnicodeError,TypeError) as exc:
        invalid(value,exc);return _urlsplit('')

def urlparse(value,scheme='',allow_fragments=True):
    try:return _checked(value,_urlparse,scheme,allow_fragments)
    except (ValueError,UnicodeError,TypeError) as exc:
        invalid(value,exc);return _urlparse('')

def valid(value,absolute=False):
    if not isinstance(value,str) or not value:return False
    try:
        p=_checked(value,_urlsplit)
        if absolute and (p.scheme not in ('http','https') or not p.hostname):raise ValueError('Absolute HTTP URL required')
        return True
    except (ValueError,UnicodeError,TypeError) as exc:invalid(value,exc);return False

def urljoin(base,url,allow_fragments=True):
    try:
        _checked(base,_urlsplit);_checked(url,_urlsplit)
        result=_urljoin(base,url,allow_fragments)
        _checked(result,_urlsplit)
        return result
    except (ValueError,UnicodeError,TypeError) as exc:
        invalid(url,exc);return ''

def urlunsplit(parts):
    try:
        result=_urlunsplit(parts);_checked(result,_urlsplit);return result
    except (ValueError,UnicodeError,TypeError) as exc:invalid('',exc);return ''
