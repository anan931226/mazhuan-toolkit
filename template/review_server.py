#!/usr/bin/env python3
"""通用的逐題人工審閱頁：把一份 JSON 攤開來一題一題審，判決另存。

這是 docs/02-human-review.md 描述那套做法的可直接執行版本。只用標準庫，沒有任何
第三方套件，複製這一個檔案就能跑。

    python3 review_server.py --items questions.json --id-field id \
        --show question,options,answer,explanation --port 8770

然後開 http://127.0.0.1:8770/

三個不可妥協的設計（每一個都是踩過才定下來的，理由見 docs/02-human-review.md）：
  1. **來源資料永遠唯讀。** 判決寫進另一個檔案，用項目 id 當 key。
  2. **判決檔用項目自己的 id 當 key，不用順序。** 刪掉項目時留空號，不要重編。
  3. **每按一次就立刻存檔。** 關掉重開要能從沒審到的地方繼續。

判決檔格式：
    { "<項目 id>": { "decision": "ok|fix|doubt", "note": "...", "at": "<ISO 時間>" } }
"""
import argparse
import html
import json
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

STATE = {}          # 執行期設定，由 main() 填入


def load_items(path, id_field):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    # 容許兩種常見結構：直接是陣列，或包在某個 key 底下
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    if not isinstance(data, list):
        raise SystemExit("--items 的內容必須是陣列，或是某個 key 底下是陣列的物件")
    missing = [i for i, r in enumerate(data) if not str(r.get(id_field, "")).strip()]
    if missing:
        raise SystemExit(
            "有 %d 筆缺少 id 欄位「%s」（第一筆在索引 %d）。"
            "判決是用 id 當 key 的，沒有穩定的 id 就沒辦法續做。" % (len(missing), id_field, missing[0])
        )
    ids = [str(r[id_field]) for r in data]
    dup = {x for x in ids if ids.count(x) > 1}
    if dup:
        raise SystemExit("id 重複：%s。先修好再審，否則判決會互相覆蓋。" % sorted(dup)[:5])
    return data


def load_decisions(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_decisions(path, decisions):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(decisions, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)   # 換名是原子操作，中途斷電不會留下半份判決檔


def render_value(v):
    """把任意欄位值渲染成可讀的 HTML。圖片路徑會直接顯示成圖。"""
    if isinstance(v, list):
        return "".join("<div class=li>%s</div>" % render_value(x) for x in v)
    if isinstance(v, dict):
        return "".join("<div class=li><b>%s</b> %s</div>" % (html.escape(str(k)), render_value(x))
                       for k, x in v.items())
    s = str(v)
    if re.search(r"\.(png|jpe?g|webp|gif)$", s, re.I):
        return '<img src="/asset?p=%s" alt="">' % html.escape(s, quote=True)
    return html.escape(s).replace("\n", "<br>")


PAGE = """<!doctype html><html lang=zh-Hant><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>逐題審閱</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:15px/1.7 -apple-system,"Noto Sans TC",sans-serif;background:#f6f4ec;color:#22221b}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #dad5c5;padding:10px 16px;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.prog{font:13px ui-monospace,monospace;color:#68655a}
.prog b{color:#22221b}
main{max-width:760px;margin:0 auto;padding:22px 16px 120px}
.field{margin:0 0 16px}
.field>.k{font:11px ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;color:#98958a;margin-bottom:3px}
.field>.v{background:#fff;border:1px solid #dad5c5;border-radius:8px;padding:11px 13px}
.li{margin:3px 0}
img{max-width:100%;border-radius:6px;border:1px solid #dad5c5;margin:4px 0}
.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #dad5c5;padding:10px 16px;
  display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid #dad5c5;background:#fff;cursor:pointer}
button.ok{background:#2a6f63;color:#fff;border-color:#2a6f63}
button.fix{background:#b23a2e;color:#fff;border-color:#b23a2e}
button.doubt{background:#a2701e;color:#fff;border-color:#a2701e}
button:disabled{opacity:.4;cursor:default}
textarea{flex:1;min-width:200px;font:inherit;padding:7px 9px;border:1px solid #dad5c5;border-radius:6px;resize:vertical}
.done{background:#dceae4;border:1px solid #2a6f63;border-radius:8px;padding:9px 12px;margin:0 0 16px;font-size:13.5px}
.kbd{font:11px ui-monospace,monospace;color:#98958a}
</style>
<header>
  <span class=prog>第 <b id=pos></b> / <span id=total></span> 題</span>
  <span class=prog>已審 <b id=done></b>，還剩 <b id=left></b></span>
  <label class=prog><input type=checkbox id=pending> 只看還沒審的</label>
  <button id=prev>上一題</button><button id=next>下一題</button>
  <span class=kbd>快捷鍵：1 可以 · 2 要改 · 3 存疑 · ← → 換題</span>
</header>
<main><div id=body></div></main>
<div class=bar>
  <button class=ok data-d=ok>1 可以</button>
  <button class=fix data-d=fix>2 要改</button>
  <button class=doubt data-d=doubt>3 存疑</button>
  <textarea id=note rows=1 placeholder="修改意見（可留空）"></textarea>
</div>
<script>
var ITEMS=[], DEC={}, idx=0, pendingOnly=false;
function view(){ return pendingOnly ? ITEMS.filter(function(x){return !DEC[x.id];}) : ITEMS; }
function cur(){ var v=view(); if(!v.length) return null; if(idx>=v.length) idx=v.length-1; return v[idx]; }
function draw(){
  var v=view(), it=cur();
  document.getElementById('total').textContent=v.length;
  document.getElementById('pos').textContent=v.length?(idx+1):0;
  var d=Object.keys(DEC).length;
  document.getElementById('done').textContent=d;
  document.getElementById('left').textContent=ITEMS.length-d;
  if(!it){ document.getElementById('body').innerHTML='<div class=done>全部審完了。</div>'; return; }
  var prev=DEC[it.id];
  document.getElementById('body').innerHTML=
    (prev?'<div class=done>已標記為 <b>'+prev.decision+'</b>'+(prev.note?'：'+prev.note:'')+'（'+prev.at.slice(0,16).replace('T',' ')+'）</div>':'')
    + it.html;
  document.getElementById('note').value=prev&&prev.note?prev.note:'';
}
function decide(d){
  var it=cur(); if(!it) return;
  var note=document.getElementById('note').value;
  fetch('/decide',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:it.id,decision:d,note:note})})
    .then(function(r){return r.json();}).then(function(j){
      DEC=j.decisions;
      if(!pendingOnly) idx=Math.min(idx+1, ITEMS.length-1);
      document.getElementById('note').value='';
      draw();
    });
}
document.querySelectorAll('[data-d]').forEach(function(b){
  b.addEventListener('click',function(){decide(b.getAttribute('data-d'));});
});
document.getElementById('prev').addEventListener('click',function(){ if(idx>0){idx--;draw();} });
document.getElementById('next').addEventListener('click',function(){ if(idx<view().length-1){idx++;draw();} });
document.getElementById('pending').addEventListener('change',function(){ pendingOnly=this.checked; idx=0; draw(); });
document.addEventListener('keydown',function(e){
  if(e.target.tagName==='TEXTAREA') return;
  if(e.key==='1') decide('ok'); else if(e.key==='2') decide('fix'); else if(e.key==='3') decide('doubt');
  else if(e.key==='ArrowLeft'){ if(idx>0){idx--;draw();} }
  else if(e.key==='ArrowRight'){ if(idx<view().length-1){idx++;draw();} }
});
fetch('/data').then(function(r){return r.json();}).then(function(j){
  ITEMS=j.items; DEC=j.decisions; draw();
});
</script>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass  # 審一整天不需要滿螢幕的 request log

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/data":
            items = []
            for r in STATE["items"]:
                rid = str(r[STATE["id_field"]])
                parts = []
                for f in STATE["fields"]:
                    if f in r and r[f] not in (None, "", [], {}):
                        parts.append('<div class=field><div class=k>%s</div><div class=v>%s</div></div>'
                                     % (html.escape(f), render_value(r[f])))
                items.append({"id": rid, "html": "".join(parts) or "<i>（這一筆沒有要顯示的欄位）</i>"})
            return self._send(200, json.dumps({"items": items, "decisions": STATE["decisions"]},
                                              ensure_ascii=False))
        if u.path == "/asset":
            # 只允許讀 --asset-dir 底下的檔案，避免這個本機頁面變成任意檔案讀取的洞
            rel = (parse_qs(u.query).get("p") or [""])[0]
            base = STATE["asset_dir"]
            if not base:
                return self._send(404, b"", "text/plain")
            full = os.path.realpath(os.path.join(base, rel))
            if not full.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(full):
                return self._send(404, b"", "text/plain")
            ext = os.path.splitext(full)[1].lower().lstrip(".")
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), "image/" + ("jpeg" if ext == "jpg" else ext))
        return self._send(404, b"", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/decide":
            return self._send(404, b"", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        rid = str(payload.get("id", ""))
        if rid:
            STATE["decisions"][rid] = {
                "decision": payload.get("decision", ""),
                "note": (payload.get("note") or "").strip(),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            save_decisions(STATE["dec_path"], STATE["decisions"])   # 每按一次就落地
        self._send(200, json.dumps({"decisions": STATE["decisions"]}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True, help="待審資料（JSON，唯讀，絕不會被寫入）")
    ap.add_argument("--id-field", default="id", help="哪個欄位當項目 id（預設 id）")
    ap.add_argument("--show", default="", help="要顯示的欄位，逗號分隔；留空表示全部")
    ap.add_argument("--decisions", default=None, help="判決檔（預設 <items>.decisions.json）")
    ap.add_argument("--asset-dir", default=None, help="圖片的根目錄，有圖要顯示時才需要")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    items = load_items(args.items, args.id_field)
    fields = [f.strip() for f in args.show.split(",") if f.strip()]
    if not fields:
        seen = []
        for r in items:
            for k in r:
                if k not in seen and k != args.id_field:
                    seen.append(k)
        fields = seen

    dec_path = args.decisions or (os.path.splitext(args.items)[0] + ".decisions.json")
    STATE.update(items=items, id_field=args.id_field, fields=fields,
                 decisions=load_decisions(dec_path), dec_path=dec_path,
                 asset_dir=args.asset_dir)

    print("待審 %d 筆，已有判決 %d 筆" % (len(items), len(STATE["decisions"])))
    print("判決寫進 %s（來源檔 %s 不會被動到）" % (dec_path, args.items))
    print("開 http://127.0.0.1:%d/ 開始審，Ctrl-C 結束" % args.port)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
