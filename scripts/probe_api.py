import json, pathlib, urllib.parse, urllib.request

BASE = "https://sec.battleoftheforms.com/api/sec-contracts/search"
params = urllib.parse.urlencode({"q": "License Agreement", "limit": 10, "offset": 0})
req = urllib.request.Request(f"{BASE}?{params}", headers={"User-Agent": "LicenseBench feasibility POC/0.1"})
with urllib.request.urlopen(req, timeout=60) as r:
    data = json.load(r)
pathlib.Path("artifacts").mkdir(exist_ok=True)
pathlib.Path("artifacts/probe.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
print(json.dumps(data, indent=2)[:12000])
