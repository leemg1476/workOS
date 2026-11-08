"""Bounded readback of explicitly allowed public HTML sources; pages remain untrusted."""
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin,urlunsplit,parse_qsl,urlencode
from datetime import datetime,timezone
import httpx
from sqlalchemy import text
from app.domain.hashing import digest
from app.work.service import uid


class VisibleText(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.hidden=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript') and self.hidden:self.hidden-=1
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def validate_url(url,domains):
    parsed=urlsplit(url);host=(parsed.hostname or '').lower()
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443):raise ValueError('HTTPS_PUBLIC_SOURCE_REQUIRED')
    if not domains or not any(host==d.lower() or host.endswith('.'+d.lower()) for d in domains):raise ValueError('SOURCE_DOMAIN_NOT_ALLOWED')
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):raise ValueError('PRIVATE_SOURCE_ADDRESS_DENIED')
    return url


def fetch_text(url,domains):
    current=url
    deadline=time.monotonic()+8
    with httpx.Client(timeout=6,follow_redirects=False,trust_env=False) as client:
        for attempt in range(4):
            if time.monotonic()>deadline:raise ValueError('SOURCE_TIME_BUDGET_EXCEEDED')
            validate_url(current,domains)
            with client.stream('GET',current,headers={'User-Agent':'WorkOS-source-readback/1.0'}) as response:
                if response.status_code in (301,302,303,307,308):
                    current=urljoin(current,response.headers.get('location',''));continue
                response.raise_for_status()
                content_type=response.headers.get('content-type','').lower()
                if 'text/html' not in content_type and 'text/plain' not in content_type:raise ValueError('SOURCE_CONTENT_TYPE_UNSUPPORTED')
                chunks=[];size=0
                for chunk in response.iter_bytes():
                    if time.monotonic()>deadline:raise ValueError('SOURCE_TIME_BUDGET_EXCEEDED')
                    size+=len(chunk)
                    if size>512000:raise ValueError('SOURCE_TOO_LARGE')
                    chunks.append(chunk)
                raw=b''.join(chunks).decode(response.encoding or 'utf-8',errors='replace')
                if 'html' in content_type:
                    parser=VisibleText();parser.feed(raw);raw=' '.join(parser.parts)
                value=re.sub(r'\s+',' ',raw).strip()
                if not value or len(value)>64000:raise ValueError('SOURCE_TEXT_EMPTY_OR_TOO_LARGE')
                return value,content_type
    raise ValueError('SOURCE_REDIRECT_LIMIT')


def capture(engine,run_id,report,research,domains):
    urls=[]
    for row in research.get('citations',[]):urls.append(row['url'])
    urls+=re.findall(r'\]\((https?://[^\s)]+)\)',report)
    for call in research.get('web_calls',[]):
        action=call.get('action',{})
        if action.get('type')=='open_page' and action.get('url'):urls.append(action['url'])
    results=[]
    urls=[urlunsplit((u.scheme,u.netloc,u.path,urlencode([(k,v) for k,v in parse_qsl(u.query,keep_blank_values=True) if not k.startswith('utm_')]),'')) for u in map(urlsplit,urls)]
    for url in list(dict.fromkeys(urls))[:3]:
        try:
            value,content_type=fetch_text(url,domains);snapshot=uid();sha=digest(value)
            with engine.begin() as c:
                c.execute(text('''INSERT INTO report_source_snapshots(snapshot_id,run_id,url,text_content,text_sha256,content_type)
                    VALUES (:id,:run,:url,:content,:sha,:type)'''),
                    {'id':snapshot,'run':run_id,'url':url,'content':value,'sha':sha,'type':content_type})
            results.append({'snapshot_id':snapshot,'url':url,'text_sha256':sha,'status':'CAPTURED',
                            'captured_at':datetime.now(timezone.utc).isoformat()})
        except (httpx.HTTPError,ValueError,OSError) as exc:
            reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            results.append({'url':url,'status':'UNAVAILABLE','reason':reason[:100]})
    return results
