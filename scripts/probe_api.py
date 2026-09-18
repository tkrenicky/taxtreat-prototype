import json, urllib.request
CID="5303df3b6937904ee2f2867ea9d1eea38fc7e818b2d468b08cba087e38758fea"
META=f"https://sec.battleoftheforms.com/api/sec-contracts/{CID}"
UA="LicenseBench-POC/0.1 (github.com/tkrenicky/taxtreat-prototype; research contact)"

def req(url, accept="*/*"):
    r=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":accept,"Accept-Encoding":"identity"})
    try:
        with urllib.request.urlopen(r,timeout=30) as x:
            raw=x.read()
            return {"ok":True,"status":x.status,"content_type":x.headers.get("Content-Type"),"bytes":len(raw),"head":raw[:1000].decode("utf-8","replace")}
    except Exception as e:
        return {"ok":False,"error":repr(e)}

meta=req(META,"application/json")
print("METADATA_RESPONSE",json.dumps(meta,indent=2))
if meta.get("ok"):
    try:
        data=json.loads(meta["head"] if meta["bytes"]<=1000 else urllib.request.urlopen(urllib.request.Request(META,headers={"User-Agent":UA})).read())
        print("METADATA_KEYS",list(data.keys()) if isinstance(data,dict) else type(data).__name__)
        print("METADATA_FULL",json.dumps(data,indent=2)[:15000])
    except Exception as e: print("META_PARSE_ERROR",repr(e))
SEC="https://www.sec.gov/Archives/edgar/data/1012870/000101287001503256/dex109.txt"
print("SEC_RESPONSE",json.dumps(req(SEC,"text/plain,text/html,*/*"),indent=2))
