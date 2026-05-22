#!/usr/bin/env python3
import argparse, json, os, re, sys, urllib.parse
import requests

HEADERS={"User-Agent":"Mozilla/5.0","Accept-Language":"zh-CN,zh;q=0.9,en;q=0.8"}
BLOCK_MARKERS=["/sorry/index","detected unusual traffic","异常流量","/httpservice/retry/enablejs","please click here if you are not redirected","如果您在几秒钟内没有被重定向"]

class ProviderUnavailable(Exception): pass
class ProviderBlocked(Exception): pass

def result(title,url,snippet,source):
    return {"title":title or "","url":url or "","snippet":snippet or "","source":source}

def serper(q,num):
    key=os.getenv("SERPER_API_KEY")
    if not key: raise ProviderUnavailable("SERPER_API_KEY not configured")
    r=requests.post("https://google.serper.dev/search",headers={"X-API-KEY":key,"Content-Type":"application/json"},json={"q":q,"num":num,"hl":"zh-cn","gl":"jp"},timeout=30)
    if r.status_code!=200: raise Exception(f"Serper HTTP {r.status_code}: {r.text[:300]}")
    data=r.json(); items=data.get("organic") or []
    out=[result(i.get("title"),i.get("link"),i.get("snippet"),"serper_google") for i in items if i.get("link")]
    if not out: raise Exception("Serper returned no organic results")
    return out[:num]

def brave(q,num):
    key=os.getenv("BRAVE_API_KEY")
    if not key: raise ProviderUnavailable("BRAVE_API_KEY not configured")
    r=requests.get("https://api.search.brave.com/res/v1/web/search",headers={"X-Subscription-Token":key,"Accept":"application/json"},params={"q":q,"count":min(num,20),"search_lang":"zh-hans","country":"JP"},timeout=30)
    if r.status_code!=200: raise Exception(f"Brave HTTP {r.status_code}: {r.text[:300]}")
    data=r.json(); items=((data.get("web") or {}).get("results")) or []
    out=[result(i.get("title"),i.get("url"),i.get("description"),"brave") for i in items if i.get("url")]
    if not out: raise Exception("Brave returned no web results")
    return out[:num]

def diagnose_google(q):
    r=requests.get("https://www.google.com/search",params={"q":q,"num":10,"hl":"zh-CN","pws":"0"},headers=HEADERS,timeout=20,allow_redirects=True)
    text=(r.url+"\n"+r.text).lower()
    markers=[m for m in BLOCK_MARKERS if m.lower() in text]
    return {"status_code":r.status_code,"final_url":r.url,"blocked_or_js_gated":bool(markers),"markers":markers,"html_length":len(r.text)}

def run(q,num,provider,diag):
    diagnostics=[]
    providers=[]
    if provider in ("auto","serper"): providers.append(("serper",serper))
    if provider in ("auto","brave"): providers.append(("brave",brave))
    if provider=="all": providers=[("serper",serper),("brave",brave)]
    outputs={}
    for name,fn in providers:
        try: outputs[name]=fn(q,num)
        except Exception as e: diagnostics.append({"provider":name,"status":"failed","error":str(e)})
    google_diag=diagnose_google(q) if diag else None
    if provider=="auto":
        chosen="serper" if outputs.get("serper") else ("brave" if outputs.get("brave") else None)
        return {"query":q,"provider":chosen,"results":outputs.get(chosen,[]),"all_provider_counts":{k:len(v) for k,v in outputs.items()},"diagnostics":diagnostics,"google_diagnostic":google_diag}
    return {"query":q,"provider":provider,"results_by_provider":outputs,"all_provider_counts":{k:len(v) for k,v in outputs.items()},"diagnostics":diagnostics,"google_diagnostic":google_diag}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("query", nargs="+")
    p.add_argument("--num",type=int,default=10)
    p.add_argument("--provider",choices=["auto","serper","brave","all"],default="auto")
    p.add_argument("--diagnose-google",action="store_true")
    a=p.parse_args(); q=" ".join(a.query)
    print(json.dumps(run(q,a.num,a.provider,a.diagnose_google),ensure_ascii=False,indent=2))
if __name__=="__main__": main()
