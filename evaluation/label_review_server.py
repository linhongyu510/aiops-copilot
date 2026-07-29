"""Local, explicit human-review UI for the 600-query RAG label set."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "rag_queries.jsonl"
DOCS = ROOT / "aiops-docs"


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def save_rows(path: Path, rows: list[dict]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


HTML = """<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">
<title>RAG 标签人工复核</title><style>
body{font:16px system-ui;max-width:1100px;margin:24px auto;padding:0 16px;background:#f6f7fb;color:#202124}
.card{background:white;border-radius:12px;padding:20px;margin:12px 0;box-shadow:0 2px 10px #0001}
button,input,textarea{font:inherit;padding:9px;margin:4px}button{cursor:pointer}.ok{background:#16883f;color:white}.bad{background:#c62828;color:white}
pre{white-space:pre-wrap;max-height:420px;overflow:auto;background:#f4f4f4;padding:12px}label{display:block;margin-top:8px}.muted{color:#666}
</style><body><h1>RAG 标签人工复核</h1><div id=progress></div><div class=card id=label></div>
<div class=card><b>预期来源文档</b><pre id=doc></pre></div>
<div class=card><label>复核人 <input id=reviewer placeholder=\"真实姓名或工号\"></label>
<label>备注 <textarea id=notes rows=3 cols=70></textarea></label>
<button class=ok onclick=save('approved')>A / 通过</button><button class=bad onclick=save('rejected')>R / 驳回</button>
<button onclick=move(-1)>上一条</button><button onclick=move(1)>下一条</button></div>
<script>
let rows=[],i=0; const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function init(){rows=await (await fetch('/api/rows')).json();let p=rows.findIndex(x=>x.review_status!=='approved'&&x.review_status!=='rejected');i=p<0?0:p;render()}
async function render(){let x=rows[i],done=rows.filter(x=>['approved','rejected'].includes(x.review_status)).length;
progress.textContent=`${i+1} / ${rows.length}，已复核 ${done}，待复核 ${rows.length-done}`;
label.innerHTML=`<b>${esc(x.id)}</b><p><b>查询：</b>${esc(x.query)}</p><p><b>来源：</b>${esc(x.expected_sources.join(', '))}</p><p><b>关键词：</b>${esc(x.expected_keywords.join(', '))}</p><p class=muted>类别 ${esc(x.category)} / 风格 ${esc(x.style)} / 难度 ${esc(x.difficulty)} / 当前 ${esc(x.review_status)}</p>`;
reviewer.value=x.reviewer||localStorage.reviewer||'';notes.value=x.review_notes||'';doc.textContent='加载中…';
doc.textContent=await (await fetch('/api/source?id='+encodeURIComponent(x.id))).text()}
function move(n){i=Math.max(0,Math.min(rows.length-1,i+n));render()}
async function save(status){let reviewerValue=reviewer.value.trim();if(!reviewerValue){alert('请填写真实复核人');return}localStorage.reviewer=reviewerValue;
let res=await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:rows[i].id,status,reviewer:reviewerValue,notes:notes.value})});
if(!res.ok){alert(await res.text());return}rows[i]=await res.json();move(1)}
document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;if(e.key.toLowerCase()==='a')save('approved');if(e.key.toLowerCase()==='r')save('rejected');if(e.key==='ArrowLeft')move(-1);if(e.key==='ArrowRight')move(1)});init();
</script></body></html>"""


def make_handler(dataset: Path):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, content: str, content_type: str) -> None:
            data = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send(200, HTML, "text/html")
                return
            rows = load_rows(dataset)
            if parsed.path == "/api/rows":
                self.send(200, json.dumps(rows, ensure_ascii=False), "application/json")
                return
            if parsed.path == "/api/source":
                query_id = parsed.query.removeprefix("id=")
                from urllib.parse import unquote

                query_id = unquote(query_id)
                row = next((item for item in rows if item.get("id") == query_id), None)
                if not row:
                    self.send(404, "unknown id", "text/plain")
                    return
                parts = []
                for source in row.get("expected_sources", []):
                    path = DOCS / source
                    parts.append(f"===== {source} =====\n" + path.read_text(encoding="utf-8"))
                self.send(200, "\n\n".join(parts), "text/plain")
                return
            self.send(404, "not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/review":
                self.send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            if payload.get("status") not in {"approved", "rejected"}:
                self.send(400, "invalid status", "text/plain")
                return
            if not str(payload.get("reviewer", "")).strip():
                self.send(400, "reviewer is required", "text/plain")
                return
            rows = load_rows(dataset)
            row = next((item for item in rows if item.get("id") == payload.get("id")), None)
            if not row:
                self.send(404, "unknown id", "text/plain")
                return
            row.update(
                review_status=payload["status"],
                reviewer=str(payload["reviewer"]).strip(),
                review_notes=str(payload.get("notes", "")).strip(),
            )
            save_rows(dataset, rows)
            self.send(200, json.dumps(row, ensure_ascii=False), "application/json")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.dataset))
    url = f"http://{args.host}:{args.port}"
    print(f"label review UI: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
