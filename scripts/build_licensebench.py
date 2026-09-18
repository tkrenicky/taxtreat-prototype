import concurrent.futures
import datetime as dt
import html
import json
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from remotezip import RemoteZip

API = "https://sec.battleoftheforms.com/api/sec-contracts/search"
OUT = pathlib.Path("artifacts")
RAW = OUT / "raw"
DOCS = pathlib.Path("docs")
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)
DOCS.mkdir(exist_ok=True)

USER_AGENT = "LicenseBench-POC/0.1 (public research; github.com/tkrenicky/taxtreat-prototype)"
TARGET = 1000
SEARCH_QUERIES = [
    "License Agreement",
    "Patent License Agreement",
    "Trademark License Agreement",
    "Technology License Agreement",
    "Software License Agreement",
    "Exclusive License Agreement",
    "Collaboration and License Agreement",
    "License and Supply Agreement",
    "Intellectual Property License Agreement",
    "Sublicense Agreement",
    "License and Development Agreement",
    "Research and License Agreement",
]

_rate_re = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,4})?)\s*(?:%|percent\b)", re.I)
_space_re = re.compile(r"\s+")
_tag_re = re.compile(r"<[^>]+>")
_script_re = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_redaction_re = re.compile(r"\[(?:\*{2,}|x{2,}|redacted|confidential[^\]]*)\]|<redacted>|\*{4,}", re.I)
_money_re = re.compile(r"(?:US\$|USD|\$|€|EUR\s*)\s?([0-9][0-9,]*(?:\.\d+)?)\s*(million|m|thousand|k)?", re.I)

_lock = threading.Lock()
_last_request = 0.0

def throttled_open(req, timeout=45):
    global _last_request
    # conservative global ceiling of roughly 5 requests/second
    with _lock:
        now = time.monotonic()
        wait = 0.21 - (now - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    return urllib.request.urlopen(req, timeout=timeout)

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with throttled_open(req) as r:
        return json.load(r)

def search_candidates():
    unique = {}
    query_counts = {}
    for query in SEARCH_QUERIES:
        total_seen = 0
        for offset in range(0, 500, 100):
            params = urllib.parse.urlencode({"q": query, "limit": 100, "offset": offset})
            data = get_json(f"{API}?{params}")
            results = data.get("results", [])
            total_seen += len(results)
            for row in results:
                cid = row.get("contract_id")
                if cid and cid not in unique:
                    row["_discovery_query"] = query
                    unique[cid] = row
            if len(results) < 100 or offset + len(results) >= int(data.get("candidate_count") or 0):
                break
        query_counts[query] = total_seen
        print(f"DISCOVERY {query}: {total_seen} returned; {len(unique)} unique total", flush=True)
        if len(unique) >= 1400:
            break
    return list(unique.values()), query_counts

def normalize_text(raw, content_type, url):
    is_pdf = raw.startswith(b"%PDF") or ".pdf" in url.lower() or "application/pdf" in (content_type or "").lower()
    if is_pdf:
        return "", "pdf", True
    try:
        s = raw.decode("utf-8", errors="replace")
    except Exception:
        s = raw.decode("latin-1", errors="replace")
    lower = s[:5000].lower()
    looks_html = "<html" in lower or "<body" in lower or bool(re.search(r"<(?:p|div|table|br|font)\b", lower))
    if looks_html:
        s = _script_re.sub(" ", s)
        s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
        s = re.sub(r"</(?:p|div|tr|li|h[1-6])\s*>", "\n", s, flags=re.I)
        s = _tag_re.sub(" ", s)
        s = html.unescape(s)
        fmt = "html"
    else:
        # EDGAR .txt frequently contains light SGML/HTML wrappers
        if "<DOCUMENT>" in s[:20000].upper() or "<TEXT>" in s[:20000].upper():
            s = _tag_re.sub(" ", s)
            s = html.unescape(s)
        fmt = "text"
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    text_chars = sum(ch.isalnum() for ch in s[:20000])
    image_only = text_chars < 300
    return s.strip(), ("image-wrapper" if image_only else fmt), image_only

def snippet(text, start, end, radius=330):
    a = max(0, start-radius)
    b = min(len(text), end+radius)
    return _space_re.sub(" ", text[a:b]).strip()[:800]

def extract_record(meta, text, fmt, ocr_required, status_code, raw_bytes, error=None):
    title = meta.get("agreement_title") or meta.get("title") or ""
    lower = text.lower()
    redacted = bool(_redaction_re.search(text))
    rates = []
    evidence = []
    royalty_positions = [m.start() for m in re.finditer(r"\broyalt(?:y|ies)\b", lower)]
    for pos in royalty_positions[:80]:
        window_start = max(0, pos - 500)
        window_end = min(len(text), pos + 800)
        window = text[window_start:window_end]
        for m in _rate_re.finditer(window):
            value = float(m.group(1))
            if 0 <= value <= 100:
                absolute_start = window_start + m.start()
                absolute_end = window_start + m.end()
                ev = snippet(text, absolute_start, absolute_end)
                item = {"rate": value, "evidence": ev}
                if not any(abs(x["rate"] - value) < 1e-9 and x["evidence"] == ev for x in rates):
                    rates.append(item)
    # Conservative: only keep up to 12 evidence-bearing rate observations.
    rates = rates[:12]

    bases = []
    for label, terms in [
        ("Net Sales", ["net sales"]),
        ("Gross Sales", ["gross sales"]),
        ("Net Revenue", ["net revenue"]),
        ("Gross Revenue", ["gross revenue"]),
        ("Sublicense Income", ["sublicense income", "sublicensing income", "sublicense revenue"]),
        ("Units", ["per unit", "per product"]),
    ]:
        if any(t in lower for t in terms):
            bases.append(label)

    title_l = title.lower()
    head = lower[:60000]
    if "trademark" in title_l or "trade mark" in title_l:
        ip_type = "Trademark"
    elif "patent" in title_l:
        ip_type = "Patent"
    elif "software" in title_l:
        ip_type = "Software"
    elif "technology" in title_l or "know-how" in head or "know how" in head:
        ip_type = "Technology / know-how"
    elif "copyright" in title_l:
        ip_type = "Copyright"
    elif "sublicen" in title_l:
        ip_type = "Sublicense"
    else:
        ip_type = "Other / mixed"

    exclusive = None
    grant_area = head
    if re.search(r"\bnon[- ]exclusive\b", grant_area):
        exclusive = False
    elif re.search(r"\bexclusive(?:ly)?\b", grant_area):
        exclusive = True

    milestone = bool(re.search(r"\bmilestone(?:s)?\b", lower))
    upfront = bool(re.search(r"\b(?:upfront|up-front|initial license fee|initial payment)\b", lower))
    minimum = bool(re.search(r"\bminimum (?:annual )?royalt", lower))
    tiered = bool(re.search(r"\b(?:tier|tiered|incremental net sales|portion of net sales)\b", lower))
    sublicensing = bool(re.search(r"\bsublicen(?:se|sing|sor|see)", lower))

    # Compact evidence: first useful royalty passage and first redaction near royalty.
    if royalty_positions:
        p = royalty_positions[0]
        evidence.append({"kind": "royalty_clause", "text": snippet(text, p, p+8, 420)})
    red_match = _redaction_re.search(text)
    if red_match and ("royalt" in lower[max(0, red_match.start()-500):red_match.end()+500]):
        evidence.append({"kind": "redaction", "text": snippet(text, red_match.start(), red_match.end(), 300)})

    unique_rate_values = []
    for x in rates:
        if x["rate"] not in unique_rate_values:
            unique_rate_values.append(x["rate"])

    usable_numeric = bool(unique_rate_values)
    if usable_numeric:
        confidence = 0.92 if "royalt" in lower else 0.72
    elif redacted and "royalt" in lower:
        confidence = 0.78
    elif "royalt" in lower:
        confidence = 0.62
    else:
        confidence = 0.35

    return {
        "contract_id": meta.get("contract_id"),
        "accession": meta.get("accession"),
        "title": title,
        "company_name": meta.get("company_name") or "",
        "filing_date": meta.get("filing_date"),
        "document_type": meta.get("document_type"),
        "document_name": meta.get("document_name"),
        "source_url": meta.get("source_document_url"),
        "discovery_query": meta.get("_discovery_query"),
        "format": fmt,
        "http_status": status_code,
        "downloaded_bytes": len(raw_bytes),
        "text_chars": len(text),
        "ocr_required": bool(ocr_required),
        "download_error": error,
        "ip_type": ip_type,
        "royalty_keyword": "royalt" in lower,
        "numeric_royalty": usable_numeric,
        "rates": unique_rate_values,
        "rate_observations": rates,
        "royalty_bases": bases,
        "redacted": redacted,
        "exclusive": exclusive,
        "milestones": milestone,
        "upfront_payment": upfront,
        "minimum_royalty": minimum,
        "tiered_or_variable": tiered,
        "sublicensing": sublicensing,
        "confidence": confidence,
        "evidence": evidence,
    }

def process_archive_group(item):
    archive_url, metas = item
    out = []
    label = pathlib.Path(urllib.parse.urlparse(archive_url).path).name
    try:
        with RemoteZip(
            archive_url,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            timeout=90,
        ) as z:
            names = set(z.namelist())
            print(f"ARCHIVE_OPEN {label}: entries={len(names)} requested={len(metas)}", flush=True)
            for i, meta in enumerate(metas, 1):
                dataset = meta.get("external_dataset") or {}
                member = dataset.get("archive_member")
                cid = meta.get("contract_id")
                if not member or member not in names:
                    out.append(extract_record(meta, "", "archive-missing", False, None, b"", f"archive member missing: {member}"))
                    continue
                try:
                    raw = z.read(member)
                    (RAW / f"{cid}.txt").write_bytes(raw)
                    text, _, ocr_required = normalize_text(raw, "text/plain", member)
                    rec = extract_record(meta, text, "text-converted", ocr_required, 206, raw)
                    rec["archive_url"] = archive_url
                    rec["archive_member"] = member
                    rec["canonical_source_url"] = meta.get("source_document_url")
                    rec["source_format"] = pathlib.Path(urllib.parse.urlparse(meta.get("source_document_url") or "").path).suffix.lower().lstrip(".") or "unknown"
                    out.append(rec)
                except Exception as exc:
                    out.append(extract_record(meta, "", "archive-error", False, None, b"", f"{type(exc).__name__}: {exc}"))
                if i % 50 == 0:
                    ok = sum(1 for r in out if not r.get("download_error") and r.get("downloaded_bytes", 0) > 0)
                    print(f"ARCHIVE_PROGRESS {label}: processed={i} successful={ok}", flush=True)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        print(f"ARCHIVE_ERROR {label}: {msg}", flush=True)
        for meta in metas:
            out.append(extract_record(meta, "", "archive-error", False, None, b"", msg))
    return out


def safe_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\/")

def build_ui(records, stats):
    # Keep the UI fully self-contained so it can be opened directly or through a static preview.
    payload = safe_json({"records": records, "stats": stats})
    template = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LicenseBench — SEC Royalty POC</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#14212b;--muted:#6d7882;--line:#dde3e8;--accent:#133c55;--soft:#eaf0f4;--good:#216e52;--warn:#8a5a14;--bad:#9b2c2c}
*{box-sizing:border-box} body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
header{background:#0e2737;color:white;padding:26px 30px 22px} header h1{font-size:25px;margin:0 0 6px;letter-spacing:-.02em} header p{margin:0;color:#c7d4dc;font-size:13px}
main{max-width:1500px;margin:0 auto;padding:22px 26px 50px}
.cards{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:11px;margin-bottom:18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 15px;box-shadow:0 1px 2px #00000008}.card b{display:block;font-size:24px;letter-spacing:-.03em}.card span{font-size:12px;color:var(--muted)}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}.toolbar input,.toolbar select{height:38px;border:1px solid #cfd8de;border-radius:7px;background:white;padding:0 10px;font-size:13px}.toolbar input{flex:1;min-width:240px}.toolbar button{height:38px;border:0;border-radius:7px;background:var(--accent);color:white;padding:0 16px;font-weight:600;cursor:pointer}
.meta{display:flex;justify-content:space-between;align-items:center;margin:10px 2px;color:var(--muted);font-size:12px}
.tablewrap{overflow:auto;background:white;border:1px solid var(--line);border-radius:10px}table{border-collapse:collapse;width:100%;font-size:12px}th{position:sticky;top:0;background:#f0f3f5;text-align:left;color:#42515c;padding:10px 9px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:9px;border-bottom:1px solid #edf0f2;vertical-align:top}tbody tr{cursor:pointer}tbody tr:hover{background:#f8fbfc}.title{font-weight:650;max-width:330px}.muted{color:var(--muted)}.badge{display:inline-block;border-radius:999px;padding:3px 7px;font-size:10px;font-weight:700;background:var(--soft);white-space:nowrap}.good{background:#e7f3ed;color:var(--good)}.warn{background:#fff3dc;color:var(--warn)}.bad{background:#fdecec;color:var(--bad)}
.rates{font-weight:700}.pager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:12px}.pager button{border:1px solid var(--line);background:white;border-radius:6px;padding:7px 10px;cursor:pointer}
dialog{width:min(900px,94vw);max-height:88vh;border:0;border-radius:12px;padding:0;box-shadow:0 24px 70px #0005}dialog::backdrop{background:#10233088}.detail-head{padding:20px 22px 16px;background:#f3f6f7;border-bottom:1px solid var(--line)}.detail-head h2{font-size:20px;margin:0 0 5px}.detail-body{padding:20px 22px;overflow:auto}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:18px}.kv{border:1px solid var(--line);padding:9px;border-radius:7px}.kv label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.04em}.kv div{font-size:13px;margin-top:3px;word-break:break-word}.ev{border-left:3px solid #9db1be;background:#f7f9fa;padding:10px 12px;margin:9px 0;font-size:12px;line-height:1.45}.close{float:right;background:none;border:0;font-size:24px;cursor:pointer}.source{display:inline-block;margin-top:8px;color:#0a638f;font-weight:600;text-decoration:none}.note{font-size:11px;color:var(--muted);margin-top:15px;line-height:1.4}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.hide-mobile{display:none}}
</style>
</head>
<body>
<header><h1>LicenseBench <span style="font-weight:400;opacity:.65">POC</span></h1><p>Real SEC Exhibit 10 corpus · first 1,000 downloaded candidate licence agreements · deterministic evidence-first extraction</p></header>
<main>
<div class="cards" id="cards"></div>
<div class="toolbar">
<input id="q" placeholder="Search title, accession, rate, IP type…">
<select id="ip"><option value="">All IP types</option></select>
<select id="rate"><option value="">Any royalty status</option><option value="yes">Numeric royalty found</option><option value="no">No numeric royalty</option></select>
<select id="red"><option value="">Any redaction status</option><option value="yes">Redacted</option><option value="no">Not redacted</option></select>
<select id="fmt"><option value="">All formats</option></select>
<button id="reset">Reset</button>
</div>
<div class="meta"><span id="count"></span><span>Click a row for evidence and source</span></div>
<div class="tablewrap"><table><thead><tr><th>Date</th><th>Agreement</th><th>IP</th><th>Rate(s)</th><th>Base</th><th>Redacted</th><th>Format</th><th class="hide-mobile">Confidence</th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="pager"><button id="prev">← Prev</button><span id="page"></span><button id="next">Next →</button></div>
</main>
<dialog id="detail"><div class="detail-head"><button class="close" id="close">×</button><h2 id="dt"></h2><div class="muted" id="dsub"></div></div><div class="detail-body" id="dbody"></div></dialog>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const DATA=JSON.parse(document.getElementById('payload').textContent), all=DATA.records, S=DATA.stats;
let page=1; const pageSize=50;
const $=id=>document.getElementById(id);
function pct(n,d){return d?Math.round(n*1000/d)/10+'%':'0%'}
$('cards').innerHTML=[
['Downloaded',S.downloaded],
['Numeric royalty',S.numeric_royalty+' · '+pct(S.numeric_royalty,S.downloaded)],
['Redacted',S.redacted+' · '+pct(S.redacted,S.downloaded)],
['OCR fallback',S.ocr_required+' · '+pct(S.ocr_required,S.downloaded)],
['HTML / text',S.machine_readable+' · '+pct(S.machine_readable,S.downloaded)],
['Download errors',S.errors]
].map(x=>'<div class="card"><b>'+x[1]+'</b><span>'+x[0]+'</span></div>').join('');
[...new Set(all.map(x=>x.ip_type).filter(Boolean))].sort().forEach(v=>$('ip').insertAdjacentHTML('beforeend','<option>'+v+'</option>'));
[...new Set(all.map(x=>x.format).filter(Boolean))].sort().forEach(v=>$('fmt').insertAdjacentHTML('beforeend','<option>'+v+'</option>'));
function filtered(){
 const q=$('q').value.trim().toLowerCase(), ip=$('ip').value, rt=$('rate').value, rd=$('red').value, fmt=$('fmt').value;
 return all.filter(x=>{
  const hay=[x.title,x.accession,x.ip_type,(x.rates||[]).join(' '),x.document_type,x.discovery_query].join(' ').toLowerCase();
  return (!q||hay.includes(q))&&(!ip||x.ip_type===ip)&&(!fmt||x.format===fmt)&&(!rt||(rt==='yes')===!!x.numeric_royalty)&&(!rd||(rd==='yes')===!!x.redacted);
 });
}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(){
 const f=filtered(), pages=Math.max(1,Math.ceil(f.length/pageSize)); if(page>pages)page=pages;
 const slice=f.slice((page-1)*pageSize,page*pageSize);
 $('count').textContent=f.length.toLocaleString()+' of '+all.length.toLocaleString()+' records';
 $('page').textContent='Page '+page+' / '+pages;
 $('prev').disabled=page<=1;$('next').disabled=page>=pages;
 $('rows').innerHTML=slice.map(x=>'<tr data-id="'+esc(x.contract_id)+'"><td>'+esc(x.filing_date||'—')+'</td><td class="title">'+esc(x.title||'Untitled')+'<div class="muted">'+esc(x.document_type||'')+' · '+esc(x.accession||'')+'</div></td><td>'+esc(x.ip_type)+'</td><td class="rates">'+(x.numeric_royalty?esc(x.rates.join(', ')+'%'):'<span class="muted">—</span>')+'</td><td>'+esc((x.royalty_bases||[]).join(', ')||'—')+'</td><td>'+(x.redacted?'<span class="badge warn">YES</span>':'<span class="badge">NO</span>')+'</td><td><span class="badge '+(x.ocr_required?'bad':'good')+'">'+esc(x.format)+'</span></td><td class="hide-mobile">'+Math.round((x.confidence||0)*100)+'%</td></tr>').join('');
 document.querySelectorAll('tbody tr').forEach(tr=>tr.onclick=()=>show(all.find(x=>x.contract_id===tr.dataset.id)));
}
function show(x){
 $('dt').textContent=x.title||'Agreement'; $('dsub').textContent=(x.filing_date||'')+' · '+(x.document_type||'')+' · '+(x.accession||'');
 const flags=[['IP type',x.ip_type],['Royalty rates',(x.rates||[]).length?x.rates.join(', ')+'%':'Not extracted'],['Royalty base',(x.royalty_bases||[]).join(', ')||'Unknown'],['Exclusive',x.exclusive===null?'Unknown':x.exclusive?'Yes':'No'],['Tiered / variable',x.tiered_or_variable?'Yes':'No'],['Milestones',x.milestones?'Yes':'No'],['Upfront payment',x.upfront_payment?'Yes':'No'],['Sublicensing',x.sublicensing?'Yes':'No'],['Redacted',x.redacted?'Yes':'No'],['Format',x.format],['Text chars',Number(x.text_chars||0).toLocaleString()],['Downloaded bytes',Number(x.downloaded_bytes||0).toLocaleString()]];
 let ev=(x.rate_observations||[]).slice(0,5).map(o=>'<div class="ev"><b>'+esc(o.rate)+'%</b> — '+esc(o.evidence)+'</div>').join('');
 if(!ev) ev=(x.evidence||[]).map(o=>'<div class="ev">'+esc(o.text)+'</div>').join('')||'<div class="muted">No royalty evidence extracted in deterministic pass.</div>';
 $('dbody').innerHTML='<div class="grid">'+flags.map(k=>'<div class="kv"><label>'+esc(k[0])+'</label><div>'+esc(k[1])+'</div></div>').join('')+'</div><h3>Evidence</h3>'+ev+'<a class="source" target="_blank" rel="noopener" href="'+esc(x.source_url)+'">Open original SEC document ↗</a><div class="note">POC extraction is deliberately conservative. A percentage is labelled a numeric royalty only when it appears near royalty language. This first run is not a finished arm’s-length comparability conclusion.</div>';
 $('detail').showModal();
}
['q','ip','rate','red','fmt'].forEach(id=>$(id).addEventListener(id==='q'?'input':'change',()=>{page=1;render()}));
$('reset').onclick=()=>{$('q').value='';$('ip').value='';$('rate').value='';$('red').value='';$('fmt').value='';page=1;render()};
$('prev').onclick=()=>{if(page>1){page--;render();scrollTo(0,0)}};$('next').onclick=()=>{page++;render();scrollTo(0,0)};
$('close').onclick=()=>$('detail').close(); render();
</script>
</body></html>'''
    return template.replace("__PAYLOAD__", payload)

def main():
    started = dt.datetime.now(dt.timezone.utc)
    candidates, query_counts = search_candidates()
    print(f"DISCOVERY COMPLETE: {len(candidates)} unique candidates", flush=True)
    if len(candidates) < TARGET:
        raise SystemExit(f"Only {len(candidates)} unique candidates discovered; need {TARGET}")

    selected = candidates[: min(len(candidates), 1150)]
    groups = defaultdict(list)
    for idx, meta in enumerate(selected):
        meta["_selection_index"] = idx
        dataset = meta.get("external_dataset") or {}
        archive_url = dataset.get("archive_url")
        if archive_url:
            groups[archive_url].append(meta)
    print(f"ARCHIVE_PLAN groups={len(groups)} selected={len(selected)}", flush=True)

    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(groups)))) as ex:
        futs = [ex.submit(process_archive_group, item) for item in groups.items()]
        for fut in concurrent.futures.as_completed(futs):
            batch = fut.result()
            records.extend(r for r in batch if not r.get("download_error") and r.get("downloaded_bytes", 0) > 0)
            print(f"DOWNLOAD successful_total={len(records)}", flush=True)

    records.sort(key=lambda r: next((m.get("_selection_index", 10**9) for m in selected if m.get("contract_id")==r.get("contract_id")), 10**9))
    records = records[:TARGET]
    # Stable presentation ordering.
    records = records[:TARGET]
    records.sort(key=lambda x: (x.get("filing_date") or "", x.get("contract_id") or ""), reverse=True)

    errors = sum(1 for r in records if r.get("download_error"))
    stats = {
        "target": TARGET,
        "downloaded": len(records),
        "numeric_royalty": sum(1 for r in records if r["numeric_royalty"]),
        "redacted": sum(1 for r in records if r["redacted"]),
        "ocr_required": sum(1 for r in records if r["ocr_required"]),
        "machine_readable": sum(1 for r in records if not r["ocr_required"] and r["text_chars"] >= 300),
        "errors": errors,
        "royalty_keyword": sum(1 for r in records if r["royalty_keyword"]),
        "formats": dict(Counter(r["format"] for r in records)),
        "ip_types": dict(Counter(r["ip_type"] for r in records)),
        "query_counts": query_counts,
        "unique_candidates_discovered": len(candidates),
        "started_at": started.isoformat(),
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "engine": "licensebench-poc-v1-deterministic",
        "notes": [
            "Numeric royalty requires a percentage close to royalty language.",
            "Redaction detection is heuristic.",
            "OCR-required documents are flagged, not OCRed in v1.",
            "Canonical source is the original SEC document URL."
        ],
    }
    (OUT / "contracts.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    (DOCS / "results.json").write_text(json.dumps({"records": records, "stats": stats}, ensure_ascii=False), encoding="utf-8")
    (DOCS / "index.html").write_text(build_ui(records, stats), encoding="utf-8")
    print("FINAL_STATS " + json.dumps(stats, ensure_ascii=False), flush=True)
    if len(records) != TARGET:
        raise SystemExit(f"Expected {TARGET} successful downloads, got {len(records)}")

if __name__ == "__main__":
    main()
