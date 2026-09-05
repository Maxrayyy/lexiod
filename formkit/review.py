"""fields.json (+ 源 PDF) → 自包含的 review.html

产出是**单个 HTML 文件**：所有裁图以 data URI 内嵌，浏览器直接打开即可，
不需要起服务、不需要网络。校对完点「导出」得到 values.json。
"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, List

from .render import crop_bbox, render_pages, to_data_uri

_CSS = """
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
     background:#f5f6f8;color:#1a1d21}
header{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #dfe3e8;
       padding:12px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;
       box-shadow:0 1px 3px rgba(0,0,0,.04)}
header h1{font-size:15px;margin:0;font-weight:600}
header .meta{color:#6b7280;font-size:12px}
.spacer{flex:1}
.pill{background:#eef1f5;border-radius:999px;padding:3px 10px;font-size:12px;color:#374151}
.pill.warn{background:#fef3c7;color:#92400e}
.pill.ok{background:#d1fae5;color:#065f46}
button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #d1d5db;background:#fff;
       cursor:pointer}
button:hover{background:#f9fafb}
button.primary{background:#2563eb;color:#fff;border-color:#2563eb}
button.primary:hover{background:#1d4ed8}
input[type=search]{padding:6px 10px;border:1px solid #d1d5db;border-radius:6px;width:200px;font:inherit}
main{padding:20px;max-width:1400px;margin:0 auto}
.page-group{margin-bottom:28px}
.page-head{font-weight:600;font-size:14px;margin:0 0 10px;padding:8px 12px;background:#e8ebef;
           border-radius:6px;display:flex;gap:12px;align-items:center}
.page-head .code{font-weight:400;color:#6b7280;font-size:12px}
.field{display:grid;grid-template-columns:minmax(220px,380px) 1fr;gap:16px;background:#fff;
       border:1px solid #e3e6ea;border-radius:8px;padding:12px;margin-bottom:10px}
.field.review{border-left:4px solid #f59e0b}
.field.done{border-left:4px solid #10b981}
.field.hidden{display:none}
.crop{background:#fafbfc;border:1px solid #eceff2;border-radius:6px;overflow:auto;max-height:260px;
      display:flex;align-items:center;justify-content:center}
.crop img{max-width:100%;display:block;cursor:zoom-in}
.crop img.zoom{max-width:none;cursor:zoom-out}
.crop .nobox{color:#9ca3af;font-size:12px;padding:20px}
.body label.lb{display:block;font-weight:600;margin-bottom:2px}
.body .sub{color:#6b7280;font-size:12px;margin-bottom:8px}
.body .reason{color:#92400e;font-size:12px;background:#fffbeb;border-radius:4px;padding:4px 8px;
              margin-bottom:8px}
.body textarea,.body input[type=text]{width:100%;font:inherit;padding:7px 9px;border:1px solid #cbd2d9;
       border-radius:6px;background:#fff}
.body textarea{min-height:52px;resize:vertical}
.body input[type=text]:focus,.body textarea:focus{outline:2px solid #93c5fd;border-color:#2563eb}
.opts{display:flex;flex-wrap:wrap;gap:8px 18px;padding:6px 0}
.opts label{display:flex;gap:6px;align-items:center;cursor:pointer}
.row2{display:flex;gap:12px;align-items:center;margin-top:8px;flex-wrap:wrap}
.row2 .note{flex:1;min-width:160px}
.chg{color:#2563eb;font-size:12px;font-weight:600}
.fid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#9ca3af}
.empty{padding:40px;text-align:center;color:#9ca3af}
"""

_JS = r"""
const STORE_KEY = 'formkit:' + DOC.doc_id;
const state = {};   // field_id -> {value, options:{label:bool}, confirmed, note, edited}

function init(){
  DOC.fields.forEach(f=>{
    const o={};
    (f.options||[]).forEach(x=>o[x.label]=!!x.checked);
    state[f.field_id]={value:f.value||'',options:o,confirmed:false,note:'',edited:false};
  });
  restore();
  DOC.fields.forEach(paint);
  refresh();
}

function restore(){
  try{
    const raw = localStorage.getItem(STORE_KEY);
    if(!raw) return;
    const saved = JSON.parse(raw);
    Object.keys(saved).forEach(k=>{ if(state[k]) state[k]=Object.assign(state[k],saved[k]); });
    document.getElementById('draft').textContent = '已恢复本地草稿';
  }catch(e){ /* file:// 下 localStorage 可能被禁用，忽略即可 */ }
}
function save(){
  try{ localStorage.setItem(STORE_KEY, JSON.stringify(state)); }catch(e){}
}

function paint(f){
  const s=state[f.field_id];
  const el=document.querySelector(`[data-fid="${CSS.escape(f.field_id)}"]`);
  if(!el) return;
  const inp=el.querySelector('.val');
  if(inp && inp.value!==s.value) inp.value=s.value;
  el.querySelectorAll('.opt').forEach(c=>{ c.checked=!!s.options[c.dataset.opt]; });
  el.querySelector('.cf').checked=s.confirmed;
  const nt=el.querySelector('.note'); if(nt) nt.value=s.note;
  el.classList.toggle('done', s.confirmed);
  el.classList.toggle('review', !s.confirmed && f.needs_review);
  el.querySelector('.chg').textContent = s.edited ? '已修改' : '';
}

function onEdit(fid){
  const f=DOC.fields.find(x=>x.field_id===fid);
  const el=document.querySelector(`[data-fid="${CSS.escape(fid)}"]`);
  const s=state[fid];
  const inp=el.querySelector('.val');
  if(inp) s.value=inp.value;
  el.querySelectorAll('.opt').forEach(c=>{ s.options[c.dataset.opt]=c.checked; });
  const nt=el.querySelector('.note'); if(nt) s.note=nt.value;
  s.confirmed=el.querySelector('.cf').checked;
  // 与原始抽取值比对，决定 edited 标记 —— 入库时要区分「人工改过」和「模型原值」
  const origOpts={}; (f.options||[]).forEach(x=>origOpts[x.label]=!!x.checked);
  s.edited = (s.value!==(f.value||'')) ||
             Object.keys(origOpts).some(k=>origOpts[k]!==!!s.options[k]);
  paint(f); save(); refresh();
}

function refresh(){
  const total=DOC.fields.length;
  const done=DOC.fields.filter(f=>state[f.field_id].confirmed).length;
  const left=DOC.fields.filter(f=>f.needs_review && !state[f.field_id].confirmed).length;
  document.getElementById('prog').textContent=`已确认 ${done}/${total}`;
  document.getElementById('todo').textContent=`待复核 ${left}`;
  applyFilter();
}

function applyFilter(){
  const only=document.getElementById('onlyReview').checked;
  const q=document.getElementById('q').value.trim().toLowerCase();
  DOC.fields.forEach(f=>{
    const el=document.querySelector(`[data-fid="${CSS.escape(f.field_id)}"]`);
    if(!el) return;
    const s=state[f.field_id];
    let show=true;
    if(only && !(f.needs_review && !s.confirmed)) show=false;
    if(show && q){
      const hay=(f.label+' '+f.field_id+' '+s.value).toLowerCase();
      if(!hay.includes(q)) show=false;
    }
    el.classList.toggle('hidden', !show);
  });
  document.querySelectorAll('.page-group').forEach(g=>{
    const any=g.querySelectorAll('.field:not(.hidden)').length>0;
    g.style.display = any ? '' : 'none';
  });
}

function exportValues(){
  const values={};
  DOC.fields.forEach(f=>{
    const s=state[f.field_id];
    values[f.field_id]={
      value:s.value, options:s.options, confirmed:s.confirmed,
      note:s.note, edited:s.edited
    };
  });
  const out={
    schema_version:DOC.schema_version, doc_id:DOC.doc_id, source_pdf:DOC.source_pdf,
    reviewed_at:new Date().toISOString(),
    reviewer:document.getElementById('reviewer').value.trim(),
    values:values
  };
  const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='values.json';
  a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}

document.addEventListener('DOMContentLoaded',()=>{
  init();
  document.getElementById('onlyReview').addEventListener('change',applyFilter);
  document.getElementById('q').addEventListener('input',applyFilter);
  document.getElementById('export').addEventListener('click',exportValues);
  document.getElementById('confirmAll').addEventListener('click',()=>{
    if(!confirm('把当前可见的所有字段标记为已确认？')) return;
    DOC.fields.forEach(f=>{
      const el=document.querySelector(`[data-fid="${CSS.escape(f.field_id)}"]`);
      if(el && !el.classList.contains('hidden')){ state[f.field_id].confirmed=true; paint(f); }
    });
    save(); refresh();
  });
  document.addEventListener('click',e=>{
    if(e.target.matches('.crop img')) e.target.classList.toggle('zoom');
  });
  document.addEventListener('keydown',e=>{
    if((e.metaKey||e.ctrlKey)&&e.key==='s'){ e.preventDefault(); exportValues(); }
  });
});
"""


def _field_html(f: Dict[str, Any]) -> str:
    fid = html.escape(f["field_id"])
    label = html.escape(f.get("label") or "")
    ftype = f.get("type") or "text"
    conf = f.get("confidence") or 0.0
    reason = html.escape(f.get("review_reason") or "")
    crop = f.get("crop_data_uri") or ""

    img = (f'<img src="{crop}" alt="{label}">' if crop
           else '<div class="nobox">无 bbox，无法定位到具体区域<br>请对照原始 PDF 该页核对</div>')

    if ftype == "checkbox_group":
        opts = "".join(
            f'<label><input type="checkbox" class="opt" data-opt="{html.escape(o["label"])}" '
            f'onchange="onEdit(\'{fid}\')"> {html.escape(o["label"])}</label>'
            for o in (f.get("options") or [])
        )
        control = f'<div class="opts">{opts or "<span class=sub>（未识别到选项）</span>"}</div>'
    elif ftype in ("signature", "table"):
        control = (f'<textarea class="val" oninput="onEdit(\'{fid}\')" '
                   f'placeholder="（{ftype}）"></textarea>')
    else:
        control = f'<input type="text" class="val" oninput="onEdit(\'{fid}\')">'

    badges = [f'<span class="pill">{html.escape(ftype)}</span>',
              f'<span class="pill">置信 {conf:.2f}</span>']
    if f.get("is_handwritten"):
        badges.append('<span class="pill warn">手写</span>')
    if f.get("source") == "vlm_retry":
        badges.append('<span class="pill">已重读</span>')

    return f"""
<div class="field" data-fid="{fid}">
  <div class="crop">{img}</div>
  <div class="body">
    <label class="lb">{label} <span class="chg"></span></label>
    <div class="sub"><span class="fid">{fid}</span> &nbsp; {" ".join(badges)}</div>
    {f'<div class="reason">{reason}</div>' if reason else ''}
    {control}
    <div class="row2">
      <label><input type="checkbox" class="cf" onchange="onEdit('{fid}')"> 已确认</label>
      <input type="text" class="note" placeholder="备注（可选）" oninput="onEdit('{fid}')">
    </div>
  </div>
</div>"""


def build_html(fields_doc: Dict[str, Any]) -> str:
    by_page: Dict[int, List[Dict[str, Any]]] = {}
    for f in fields_doc["fields"]:
        by_page.setdefault(f["page"], []).append(f)

    page_meta = {p["page"]: p for p in fields_doc.get("pages", [])}
    groups = []
    for pno in sorted(by_page):
        m = page_meta.get(pno, {})
        title = html.escape(m.get("form_title") or "")
        code = html.escape(m.get("form_code") or "")
        head = (f'<div class="page-head">第 {pno} 页 &nbsp; {title}'
                f'<span class="code">{code}</span></div>')
        body = "".join(_field_html(f) for f in by_page[pno])
        groups.append(f'<section class="page-group">{head}{body}</section>')

    # crop_data_uri 不进 JS（体积太大且 JS 用不到），只在 HTML 里出现一次
    slim = {
        "schema_version": fields_doc.get("schema_version"),
        "doc_id": fields_doc["doc_id"],
        "source_pdf": fields_doc.get("source_pdf", ""),
        "fields": [
            {k: f.get(k) for k in
             ("field_id", "page", "label", "type", "value", "options", "needs_review")}
            for f in fields_doc["fields"]
        ],
    }
    # 关键：必须转义 < > &，否则 label 里一旦出现 "</script>"（OCR 完全可能
    # 从表单上读出尖括号）就会提前闭合 <script> 标签，页面直接崩掉甚至可被注入。
    # json.dumps 不会转义 '/'，所以这一步不能省。
    doc_json = (
        json.dumps(slim, ensure_ascii=False)
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    n = len(fields_doc["fields"])
    review_n = sum(1 for f in fields_doc["fields"] if f.get("needs_review"))

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>表单校对 · {html.escape(fields_doc.get('source_pdf',''))}</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>表单校对</h1>
  <span class="meta">{html.escape(fields_doc.get('source_pdf',''))} ·
    {fields_doc.get('page_count', 0)} 页 · {n} 字段 ·
    doc_id <code>{html.escape(fields_doc['doc_id'])}</code></span>
  <span class="pill ok" id="prog">已确认 0/{n}</span>
  <span class="pill warn" id="todo">待复核 {review_n}</span>
  <span class="meta" id="draft"></span>
  <div class="spacer"></div>
  <label><input type="checkbox" id="onlyReview"> 只看待复核</label>
  <input type="search" id="q" placeholder="搜索字段名 / 值">
  <input type="text" id="reviewer" placeholder="校对人" style="width:100px;padding:6px 10px;
         border:1px solid #d1d5db;border-radius:6px;font:inherit">
  <button id="confirmAll">全部确认（当前可见）</button>
  <button class="primary" id="export">导出 values.json</button>
</header>
<main>{"".join(groups) or '<div class="empty">没有字段</div>'}</main>
<script>const DOC = {doc_json};</script>
<script>{_JS}</script>
</body></html>"""


def build_review(fields_doc: Dict[str, Any], pdf_path: str,
                 dpi: int = 200, progress=lambda m: None) -> str:
    """渲染 PDF、为每个字段裁图并内嵌，返回完整 HTML 字符串。"""
    progress("渲染 PDF 用于裁图…")
    pages = render_pages(pdf_path, dpi=dpi)
    total = len(fields_doc["fields"])
    for i, f in enumerate(fields_doc["fields"], 1):
        pno = f["page"]
        if pno < 1 or pno > len(pages):
            continue
        f["crop_data_uri"] = to_data_uri(crop_bbox(pages[pno - 1], f.get("bbox")))
        if i % 25 == 0:
            progress(f"  裁图 {i}/{total}")
    progress("生成 HTML…")
    return build_html(fields_doc)
