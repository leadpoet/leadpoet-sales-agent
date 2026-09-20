"""Opt-in future credential route. No keys, presign mutation, or default probe."""
import os
from datetime import date,timedelta

class OptionalProvider:
    def __init__(self):
        # The wrapper may set this ONLY after the upstream contract accepts it.
        self.requested=os.environ.get('AGENT_SCRAPINGDOG_CREDENTIAL_ACCEPTED')=='1'
        self.state='unknown' if self.requested else 'disabled'
        self.used=0

    def request(self,client,endpoint,params,*,page=False):
        from arena_transport import _page_content
        from agent.deadline import BudgetExhausted
        if self.state=='disabled' or self.used>=30:return None
        probe=self.state=='unknown'
        if probe and page:return None
        claim=getattr(client,'before_optional_provider',None)
        ticket=claim(endpoint,probe) if claim else None
        self.used+=1;actual=None
        try:
            response=client._client.get('http://api.scrapingdog.com/'+endpoint,params=params,timeout=client.timeout)
            try:data=response.json()
            except ValueError:data={}
            error=data.get('error') if isinstance(data,dict) else None
            if not response.is_success or error:
                if isinstance(error,dict) and error.get('code')=='miner_provider_not_configured':actual=0
                self.state='disabled';return None
            self.state='ready'
            if page:
                from agent.v92_common import page_metadata
                title,text=_page_content(response.text,8000)
                value={'url':params['url'],'title':title,'text':text,'status_code':response.status_code,'source':'ScrapingDog',
                       **page_metadata(response.text,params['url'])}
                client._store_page(value,params['url']);return value
            if not isinstance(data,dict):self.state='disabled';return None
            return data
        except BudgetExhausted:raise
        except Exception:
            self.state='disabled';return None
        finally:
            settle=getattr(client,'settle_optional_provider',None)
            if ticket is not None and settle:settle(ticket,actual)

    def search(self,client,arguments):
        from arena_transport import _evidence_url
        if not self.requested:return None
        mode=arguments.get('mode') or 'search';query=arguments['query'][:500]
        recency=arguments.get('recency_days')
        if recency or mode=='news':
            evaluation=date.fromisoformat(os.environ.get('LAB_ARENA_EVALUATION_DATE') or os.environ.get('BAKEOFF_EVALUATION_DATE') or date.today().isoformat())
            query=query[:465]+' after:'+(evaluation-timedelta(days=int(recency or 365))).isoformat()
        endpoint={'search':'google','news':'google_news','jobs':'google_jobs'}[mode]
        with client._lock(('optional-provider',)):
            data=self.request(client,endpoint,{'query':query})
        if data is None:return None
        key={'search':'organic_results','news':'news_results','jobs':'jobs_results'}[mode]
        rows=[]
        for r in data.get(key,[]) if isinstance(data.get(key,[]),list) else []:
            if not isinstance(r,dict):continue
            url=_evidence_url(r.get('url') or r.get('link'))
            if not url:continue
            rows.append({'url':url,'title':str(r.get('title') or ''),'snippet':str(r.get('snippet') or ''),'date':r.get('date') or ''})
            if len(rows)>=min(8,int(arguments.get('limit') or 5)):break
        return {'results':rows,'count':len(rows),'mode':mode}

    def fetch(self,client,url):
        with client._lock(('optional-provider',)):
            if self.state!='ready':return None
            return self.request(client,'scrape',{'url':url,'dynamic':'false'},page=True)
