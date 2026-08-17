#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Граф как база с поиском: локальный веб-интерфейс над sync/index.db.

    python tools/graphserve.py            # http://127.0.0.1:8765
    python tools/graphserve.py 9000       # свой порт

Зачем. Граф знает про строку всё — перевод, категории словаря, файлы батчей,
чем вещь является по данным игры, дефекты, упоминания имён, — но доставать это
приходилось командами `index.py find/who/proof`, по одной строке за раз. Для
вычитки нужен обратный порядок: сначала выбрать пачку строк по признаку
(«названия предметов, где есть тип вещи, но перевод машинный»), потом читать.

Ничего, кроме стандартной библиотеки: sqlite3 + http.server. База только
читается, запись невозможна — интерфейс смотровой.
"""
import html, json, os, re, sqlite3, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
DB = os.path.join(CROWD, "sync", "index.db")
STRIP_STR = re.compile(r"%str\d+%")
STRIP_PL = re.compile(r"\[(?:s|pl:\"[^\"]*\")\]")


def item_key(s):
    s = STRIP_PL.sub("", STRIP_STR.sub("", s or ""))
    return re.sub(r"\s{2,}", " ", s).strip()


def conn():
    db = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True,
                         check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


KIND_RU = {"region": "регион", "map": "карта", "sector": "сектор",
           "landmark": "достопримечательность", "waypoint": "путевая точка",
           "vista": "обзор", "unlock": "проход", "task": "сердце почёта",
           "skill_challenge": "испытание героя", "mastery_point": "точка мастерства",
           "adventure": "приключение", "poi": "точка"}
DBC = None


def search(q, where, cat, typ, only, limit, reg=""):
    """Строки по подстроке + фильтры. Возвращает список словарей."""
    sql = ["SELECT s.hash, s.english, s.ru FROM string s"]
    args, cond = [], []
    if cat:
        sql.append("JOIN place p ON p.hash=s.hash AND p.kind='категория' AND p.ref=?")
        args.append(cat)
    if q:
        like = "%" + q + "%"
        if where == "en":
            cond.append("s.english LIKE ?"); args.append(like)
        elif where == "ru":
            cond.append("s.ru LIKE ?"); args.append(like)
        else:
            cond.append("(s.english LIKE ? OR s.ru LIKE ?)"); args += [like, like]
    if only == "notran":
        cond.append("(s.ru IS NULL OR s.ru='')")
    elif only == "defect":
        cond.append("EXISTS (SELECT 1 FROM defect d WHERE d.hash=s.hash)")
    elif only == "geo":
        cond.append("EXISTS (SELECT 1 FROM geo g WHERE g.key=s.key)")
    elif only == "latin":
        cond.append("s.ru GLOB '*[A-Za-z][A-Za-z][A-Za-z]*'")
    if cond:
        sql.append("WHERE " + " AND ".join(cond))
    sql.append("LIMIT ?")
    args.append(min(int(limit or 100), 500))
    rows = DBC.execute(" ".join(sql), args).fetchall()

    out = []
    for r in rows:
        d = {"hash": r["hash"], "en": r["english"], "ru": r["ru"] or ""}
        f = DBC.execute("SELECT kind, type, subtype, weight, rarity, level FROM item "
                        "WHERE key=? LIMIT 1", (item_key(r["english"]),)).fetchone()
        if f:
            bits = [x for x in (f["type"], f["subtype"] if f["subtype"] != f["type"] else "",
                                f["weight"], f["rarity"]) if x]
            d["fact"] = "/".join(bits) + (" · ур. %s" % f["level"] if f["level"] else "")
        else:
            d["fact"] = ""
        if typ and (not f or f["type"] != typ):
            continue
        g = DBC.execute("SELECT kind, map, region, continent FROM geo WHERE key=? LIMIT 1",
                        (item_key(r["english"]),)).fetchone()
        if g:
            d["geo"] = "%s · %s" % (KIND_RU.get(g["kind"], g["kind"]),
                                    " / ".join(x for x in (g["region"], g["map"]) if x))
            d["region"] = g["region"] or ""
        else:
            d["geo"] = d["region"] = ""
        if reg and d["region"] != reg:
            continue
        d["cats"] = [x[0] for x in DBC.execute(
            "SELECT DISTINCT name FROM place WHERE hash=? AND kind='категория'", (r["hash"],))]
        d["files"] = [x[0] for x in DBC.execute(
            "SELECT DISTINCT ref FROM place WHERE hash=? AND kind='батч' LIMIT 4", (r["hash"],))]
        d["defects"] = [x[0] for x in DBC.execute(
            "SELECT DISTINCT kind FROM defect WHERE hash=? LIMIT 6", (r["hash"],))]
        d["ctx"] = (DBC.execute("SELECT kind FROM ctx WHERE hash=?", (r["hash"],)).fetchone()
                    or [""])[0]
        out.append(d)
    return out


def detail(h):
    r = DBC.execute("SELECT english, ru FROM string WHERE hash=?", (h,)).fetchone()
    if not r:
        return {}
    d = {"en": r["english"], "ru": r["ru"] or ""}
    d["places"] = [dict(kind=x["kind"], name=x["name"], ref=x["ref"]) for x in DBC.execute(
        "SELECT kind, name, ref FROM place WHERE hash=? ORDER BY kind", (h,))]
    d["defects"] = [dict(kind=x["kind"], detail=x["detail"]) for x in DBC.execute(
        "SELECT kind, detail FROM defect WHERE hash=?", (h,))]
    d["mentions"] = [dict(en=x["en"], ru=x["ru"] or "") for x in DBC.execute(
        "SELECT m.en, e.ru FROM mention m LEFT JOIN entity e ON e.en=m.en WHERE m.hash=?", (h,))]
    d["terms"] = [dict(term=x["term"], bad=x["bad"]) for x in DBC.execute(
        "SELECT term, bad FROM term_hit WHERE hash=?", (h,))]
    d["geo"] = [dict(kind=KIND_RU.get(g["kind"], g["kind"]), map=g["map"],
                     region=g["region"], continent=g["continent"], x=g["x"], y=g["y"])
                for g in DBC.execute(
                    "SELECT kind, map, region, continent, x, y FROM geo WHERE key=? LIMIT 6",
                    (item_key(r["english"]),))]
    facts = DBC.execute("SELECT name, kind, type, subtype, weight, rarity, level FROM item "
                        "WHERE key=?", (item_key(r["english"]),)).fetchall()
    d["facts"] = [dict(zip(("name", "kind", "type", "subtype", "weight", "rarity", "level"), f))
                  for f in facts]
    return d


def cats():
    return [dict(ref=r["ref"], name=r["name"], n=r["n"]) for r in DBC.execute(
        "SELECT ref, name, count(*) n FROM place WHERE kind='категория' "
        "GROUP BY ref ORDER BY n DESC")]


PAGE = """<!doctype html><html lang=ru><meta charset=utf-8>
<title>Граф перевода GW2</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#faf9f7;--fg:#1a1a18;--dim:#6b6a66;--line:#e2e0da;--card:#fff;--accent:#8a5a2b;
      --warn:#a33;--ok:#2c6e49}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e8e6e1;--dim:#9a978f;
      --line:#2c2c33;--card:#1e1e24;--accent:#c9954a;--warn:#e0736b;--ok:#6fbf8f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
       padding:14px 18px;z-index:5}
h1{margin:0 0 10px;font-size:17px;font-weight:600;letter-spacing:.2px}
h1 span{color:var(--dim);font-weight:400;font-size:14px;margin-left:8px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=search]{flex:1;min-width:240px;padding:9px 12px;border:1px solid var(--line);
       border-radius:8px;background:var(--card);color:var(--fg);font-size:15px}
select,button{padding:8px 10px;border:1px solid var(--line);border-radius:8px;
       background:var(--card);color:var(--fg);font-size:14px}
main{padding:14px 18px 60px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.4px;
   color:var(--dim);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--card)}
.en{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.ru{font-size:14px}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:99px;
     border:1px solid var(--line);color:var(--dim);margin:1px 3px 1px 0;white-space:nowrap}
.tag.fact{color:var(--accent);border-color:var(--accent)}
.tag.bad{color:var(--warn);border-color:var(--warn)}
.tag.geo{color:var(--ok);border-color:var(--ok)}
.tag.ok{color:var(--ok);border-color:var(--ok)}
.empty{color:var(--warn)}
#det{position:fixed;right:0;top:0;bottom:0;width:min(460px,92vw);background:var(--card);
     border-left:1px solid var(--line);padding:18px;overflow:auto;transform:translateX(100%);
     transition:transform .18s ease}
#det.on{transform:none}
#det h2{margin:0 0 4px;font-size:15px}
#det .k{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.4px;
        margin:14px 0 4px}
#det pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:13px}
.x{float:right;cursor:pointer;color:var(--dim);border:0;background:0;font-size:20px}
.muted{color:var(--dim)}
</style>
<header>
  <h1>Граф перевода GW2 <span id=stat></span></h1>
  <div class=row>
    <input type=search id=q placeholder="искать в оригинале или переводе…" autofocus>
    <select id=where><option value=both>везде<option value=en>только EN<option value=ru>только RU</select>
    <select id=cat><option value="">все категории</option></select>
    <select id=type><option value="">любой предмет</option><option>Weapon</option>
      <option>Armor</option><option>Trinket</option><option>Consumable</option>
      <option>Container</option><option>Back</option><option>Gathering</option></select>
    <select id=reg><option value="">все регионы</option></select>
    <select id=only><option value="">без фильтра<option value=defect>с дефектом
      <option value=notran>без перевода<option value=latin>латиница в переводе
      <option value=geo>есть место на карте</select>
    <select id=limit><option>100</option><option>250</option><option>500</option></select>
  </div>
</header>
<main><table><thead><tr><th style=width:38%>оригинал<th style=width:38%>перевод<th>метки</tr>
</thead><tbody id=rows></tbody></table><p id=msg class=muted></p></main>
<div id=det><button class=x onclick="det.classList.remove('on')">×</button><div id=detb></div></div>
<script>
const $=s=>document.querySelector(s), rows=$('#rows'), det=$('#det');
let timer=null;
fetch('/api/regions').then(r=>r.json()).then(rs=>{
  $('#reg').innerHTML='<option value="">все регионы</option>'+
    rs.map(r=>`<option>${r}</option>`).join('');
});
fetch('/api/cats').then(r=>r.json()).then(cs=>{
  $('#cat').innerHTML='<option value="">все категории</option>'+
    cs.map(c=>`<option value="${c.ref}">${c.name} (${c.n})</option>`).join('');
});
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function go(){
  const p=new URLSearchParams({q:$('#q').value,where:$('#where').value,cat:$('#cat').value,
    type:$('#type').value,only:$('#only').value,limit:$('#limit').value,
    reg:$('#reg').value});
  $('#msg').textContent='ищу…';
  fetch('/api/search?'+p).then(r=>r.json()).then(d=>{
    rows.innerHTML=d.map(r=>`<tr onclick="open_('${r.hash}')">
      <td class=en>${esc(r.en).slice(0,300)}</td>
      <td class="ru${r.ru?'':' empty'}">${r.ru?esc(r.ru).slice(0,300):'— нет перевода —'}</td>
      <td>${r.fact?`<span class="tag fact">${esc(r.fact)}</span>`:''}
        ${r.cats.map(c=>`<span class=tag>${esc(c)}</span>`).join('')}
        ${r.defects.map(c=>`<span class="tag bad">${esc(c)}</span>`).join('')}
        ${r.ctx==='механика'?'<span class="tag ok">механика</span>':''}</td></tr>`).join('');
    $('#msg').textContent=d.length?`строк: ${d.length}`:'ничего не найдено';
  });
}
function open_(h){
  fetch('/api/string?h='+h).then(r=>r.json()).then(d=>{
    $('#detb').innerHTML=`<h2>оригинал</h2><pre>${esc(d.en)}</pre>
      <div class=k>перевод</div><pre>${esc(d.ru)||'<i>нет</i>'}</pre>
      ${d.facts.length?'<div class=k>чем является по игре</div>'+d.facts.map(f=>
        `<div>${esc([f.type,f.subtype,f.weight,f.rarity,f.level?'ур. '+f.level:''].filter(Boolean).join(' / '))}</div>`).join(''):''}
      ${d.geo.length?'<div class=k>место на карте</div>'+d.geo.map(g=>
        `<div>${esc(g.kind)} — ${esc([g.continent,g.region,g.map].filter(Boolean).join(' / '))}
         <span class=muted>${g.x?`(${g.x}, ${g.y})`:''}</span></div>`).join(''):''}
      ${d.places.length?'<div class=k>где лежит</div>'+d.places.map(p=>
        `<div class=muted>${esc(p.kind)}: ${esc(p.name)} <span class=en>${esc(p.ref)}</span></div>`).join(''):''}
      ${d.defects.length?'<div class=k>дефекты</div>'+d.defects.map(x=>
        `<div><span class="tag bad">${esc(x.kind)}</span> ${esc(x.detail)}</div>`).join(''):''}
      ${d.terms.length?'<div class=k>термины глоссария</div>'+d.terms.map(x=>
        `<div>${esc(x.term)} ${x.bad?`<span class="tag bad">${esc(x.bad)}</span>`:''}</div>`).join(''):''}
      ${d.mentions.length?'<div class=k>упомянутые имена</div>'+d.mentions.map(x=>
        `<div>${esc(x.en)} → ${esc(x.ru)}</div>`).join(''):''}`;
    det.classList.add('on');
  });
}
['input','change'].forEach(ev=>document.querySelectorAll('input,select').forEach(el=>
  el.addEventListener(ev,()=>{clearTimeout(timer);timer=setTimeout(go,220)})));
go();
</script>
</html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (p.get(k) or [d])[0]
        try:
            if u.path == "/":
                self._send(PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/regions":
                self._send(json.dumps([r[0] for r in DBC.execute(
                    "SELECT region, count(*) n FROM geo WHERE region<>'' "
                    "GROUP BY region ORDER BY n DESC")], ensure_ascii=False))
            elif u.path == "/api/cats":
                self._send(json.dumps(cats(), ensure_ascii=False))
            elif u.path == "/api/search":
                r = search(one("q"), one("where", "both"), one("cat"), one("type"),
                           one("only"), one("limit", "100"), one("reg"))
                self._send(json.dumps(r, ensure_ascii=False))
            elif u.path == "/api/string":
                self._send(json.dumps(detail(one("h")), ensure_ascii=False))
            else:
                self.send_error(404)
        except Exception as e:
            self._send(json.dumps({"error": str(e)}, ensure_ascii=False))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not os.path.exists(DB):
        sys.exit("нет %s — сначала `python tools/index.py build`" % DB)
    DBC = conn()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    n = DBC.execute("SELECT count(*) FROM string").fetchone()[0]
    print("строк в графе: %d" % n)
    print("открой http://127.0.0.1:%d  (Ctrl+C — остановить)" % port)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
