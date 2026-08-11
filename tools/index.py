#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Граф перевода: что где лежит, что с чем связано.

    python tools/index.py build              # собрать sync/index.db из bin, батчей, глоссария
    python tools/index.py find <текст>       # где лежит строка: категории словаря и файлы батчей
    python tools/index.py who <имя>          # досье на сущность: канон, где встречается, соседи
    python tools/index.py cat [<категория>]  # состав словаря или одной категории
    python tools/index.py dup                # записи в нескольких категориях с РАЗНЫМ переводом
    python tools/index.py term [<термин>]    # где встречается термин глоссария, где нарушен канон
    python tools/index.py bad [<имя|категория>]  # дефекты: сводка или по персонажу/категории
    python tools/index.py skills [проф|тип|оружие|слот]  # покрытие умений в разрезе

Зачем: батчи нарезаны корзинами сбора (`ui`, `new`, `zone`), и по имени файла не
понять, что в нём лежит — из 449 351 строки категория батча совпадает с
категорией словаря только у 80 033. Смысл несёт словарь, но связи между его
60 категориями, слоем имён и глоссарием нигде не записаны. База их и хранит.

База не версионируется: она целиком выводится из bin, батчей и GLOSSARY.md.
"""
import csv, glob, os, re, sqlite3, sys, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
DB = os.path.join(CROWD, "sync", "index.db")
sys.path.insert(0, HERE)
sys.path.insert(0, CROWD)          # validate.py лежит в корне crowdsource
import dict_tool as D
try:
    import validate as _validate
except Exception:
    _validate = None

WORD = re.compile(r"[A-Z][A-Za-z'’\-]{2,}")


def hx(h):
    """FNV-1a 64 бита беззнаковый, а целые SQLite знаковые — храним hex-строкой."""
    return "%016x" % h


def connect(create=False):
    if not create and not os.path.exists(DB):
        sys.exit("нет %s — сначала `index.py build`" % os.path.relpath(DB, CROWD))
    return sqlite3.connect(DB)


def cmd_build(_args):
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    db = connect(create=True)
    db.executescript("""
        CREATE TABLE string(hash TEXT PRIMARY KEY, english TEXT, ru TEXT);
        CREATE TABLE place(hash TEXT, kind TEXT, name TEXT, ref TEXT);
        CREATE TABLE entity(en TEXT PRIMARY KEY, ru TEXT, layer TEXT);
        CREATE TABLE mention(hash TEXT, en TEXT);
        CREATE TABLE term(en TEXT, ru TEXT, banned TEXT);
        CREATE TABLE warband(en TEXT PRIMARY KEY, wb TEXT, legion TEXT, charr TEXT);
        CREATE TABLE defect(hash TEXT, kind TEXT, detail TEXT);
        CREATE TABLE term_hit(hash TEXT, term TEXT, bad TEXT);
        CREATE TABLE ctx(hash TEXT PRIMARY KEY, kind TEXT);
        CREATE TABLE skill(id TEXT, vid TEXT, name TEXT, descr TEXT, prof TEXT,
                           type TEXT, weapon TEXT, slot TEXT,
                           name_ru TEXT, descr_ru TEXT);
        CREATE INDEX i_skill_prof ON skill(prof);
        CREATE INDEX i_skill_w ON skill(weapon);
        CREATE INDEX i_place ON place(hash);
        CREATE INDEX i_place_name ON place(name);
        CREATE INDEX i_mention ON mention(en);
        CREATE INDEX i_mention_h ON mention(hash);
        CREATE INDEX i_defect ON defect(hash);
        CREATE INDEX i_defect_k ON defect(kind);
        CREATE INDEX i_term_hit ON term_hit(term);
        CREATE INDEX i_term_hit_h ON term_hit(hash);
    """)

    strings, places = {}, []
    ents = {}
    for name, es in D.read_sections(D.OUR_BIN):
        cat, _, disp = name.partition("\x1f")
        for h, en, ru in es:
            if h not in strings or (en and not strings[h][0]):
                strings[h] = (en, ru)
            places.append((hx(h), "категория", disp or cat, cat))
            if cat.startswith("pn_") and en and ru:
                ents[en.strip()] = (ru.strip(), cat)
    print("записей словаря: %d | сущностей в слое: %d" % (len(strings), len(ents)))

    nb = 0
    for fp in sorted(glob.glob(os.path.join(CROWD, "*", "*.csv"))):
        rows = list(csv.reader(open(fp, encoding="utf-8")))
        if not rows or rows[0][:1] != ["english"]:
            continue
        rel = os.path.relpath(fp, CROWD).replace("\\", "/")
        for i, r in enumerate(rows[1:], start=2):
            if not r or not r[0].strip():
                continue
            h = D.fnv1a_u16(r[0])
            strings.setdefault(h, (r[0], r[1] if len(r) > 1 else ""))
            places.append((hx(h), "батч", os.path.basename(fp), "%s:%d" % (rel, i)))
            nb += 1
    print("строк батчей: %d" % nb)

    db.executemany("INSERT INTO string VALUES (?,?,?)",
                   ((hx(h), en, ru) for h, (en, ru) in strings.items()))
    db.executemany("INSERT INTO place VALUES (?,?,?,?)", places)
    db.executemany("INSERT INTO entity VALUES (?,?,?)",
                   ((en, ru, lay) for en, (ru, lay) in ents.items()))

    # Упоминания: в лоб 14k имён на 460k строк не считаются, поэтому строим
    # обратный индекс по первому слову имени и проверяем только кандидатов.
    # Регекспы компилируем ОДИН раз: кеш re держит 512 шаблонов, а имён 14 тысяч,
    # и на строковых шаблонах он молотит вхолостую — сборка не заканчивается.
    by_first = collections.defaultdict(list)
    for en in ents:
        w = WORD.findall(en)
        if w:
            by_first[w[0]].append(
                (en, re.compile(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])")))
    ment = []
    for h, (en, _ru) in strings.items():
        if not en:
            continue
        seen = set()
        for w in set(WORD.findall(en)):
            for cand, rx in by_first.get(w, ()):
                if cand not in seen and cand != en and rx.search(en):
                    seen.add(cand)
        ment += [(hx(h), c) for c in seen]
    db.executemany("INSERT INTO mention VALUES (?,?)", ment)
    print("связей «строка упоминает имя»: %d" % len(ment))

    terms = []
    gl = os.path.join(CROWD, "GLOSSARY.md")
    if os.path.exists(gl):
        for line in open(gl, encoding="utf-8"):
            c = [x.strip() for x in line.split("|")]
            if len(c) < 4 or c[1].startswith("---") or c[1].lower() == "en":
                continue
            bans = " / ".join(m.group(1) for m in re.finditer(r"~~([^~]+)~~", c[3]))
            if c[1] and c[2]:
                terms.append((c[1], c[2], bans))
    db.executemany("INSERT INTO term VALUES (?,?,?)", terms)
    print("терминов глоссария: %d" % len(terms))

    # лор с официальной вики: отряд и легион чарра (tools/wikilore.py fetch)
    wb = []
    wfp = os.path.join(CROWD, "sync", "api", "charr_wiki.csv")
    if os.path.exists(wfp):
        for r in list(csv.reader(open(wfp, encoding="utf-8-sig")))[1:]:
            if len(r) >= 5 and (r[2] or r[3] or r[4]):
                wb.append((r[0], r[2], r[3], r[4]))
    # состав отрядов со страниц вики (tools/wikilore.py rosters) — он полнее,
    # чем то, что видно во вступлении статьи про самого чарра
    rfp = os.path.join(CROWD, "sync", "api", "warband_roster.csv")
    if os.path.exists(rfp):
        known = {x[0] for x in wb}
        for r in list(csv.reader(open(rfp, encoding="utf-8-sig")))[1:]:
            if len(r) >= 2 and r[1] and r[1] not in known:
                wb.append((r[1], r[0].replace(" Warband", "").replace(" warband", ""),
                           "", ""))
                known.add(r[1])
    db.executemany("INSERT OR IGNORE INTO warband VALUES (?,?,?,?)", wb)
    print("чарров с лором вики: %d" % len(wb))

    # Дефекты: тот же разбор, что у charscan.check и validate, но привязанный к
    # строке. Тогда можно спросить не «сколько всего сломано», а «что сломано у
    # этого персонажа» или «в этой категории».
    dfx = []
    try:
        import charscan as C
        checks = C.CHECKS
    except Exception as e:
        checks = []
        print("  charscan не подключился: %s" % e)
    for h, (en, ru) in strings.items():
        if not en or not ru:
            continue
        for kind, sev, fn in checks:
            try:
                d = fn(en, ru)
            except Exception:
                continue
            if d:
                dfx.append((hx(h), kind, str(d)[:120]))
        if _validate is not None:
            for e in _validate.check_row(en, ru)[0]:
                dfx.append((hx(h), "линтер", e[:120]))
    db.executemany("INSERT INTO defect VALUES (?,?,?)", dfx)
    print("дефектов привязано к строкам: %d" % len(dfx))

    # Контекст строки. Канон боевых терминов обязателен в механическом тексте и
    # НЕ обязателен в художественном: «the foolish vigor of youth» — обычное
    # слово, там «энергия юности» вернее «энергичности». Границу берём у игры:
    # что API отдаёт по /skills и /traits, то механика (tools/apicat.py mech).
    mech_en = set()
    mfp = os.path.join(CROWD, "sync", "api", "mechanical.csv")
    if os.path.exists(mfp):
        for r in list(csv.reader(open(mfp, encoding="utf-8-sig")))[1:]:
            if r and r[0].strip():
                mech_en.add(r[0].strip())
    MECH_CAT = {"skills", "traits", "specializations", "itemstats", "professions"}
    FLAV_CAT = {"npc_dialogue", "personal_story", "zone_dialogue", "backstory",
                "stories", "stories_seasons"}
    cat_of = collections.defaultdict(set)
    for hh, _k, _n, ref in places:
        cat_of[hh].add(ref)
    ctx = []
    for h, (en, _ru) in strings.items():
        hh = hx(h)
        cats = cat_of.get(hh, set())
        if en in mech_en or (cats & MECH_CAT) or (en and "<c=@abilitytype>" in en):
            k = "механика"
        elif (en and "<c=@flavor>" in en) or (cats & FLAV_CAT):
            k = "художественный"
        else:
            k = "прочее"
        ctx.append((hh, k))
    db.executemany("INSERT OR REPLACE INTO ctx VALUES (?,?)", ctx)
    ctx_of = dict(ctx)
    cnt = collections.Counter(k for _h, k in ctx)
    print("контекст строк: " + " | ".join("%s %d" % (k, v) for k, v in cnt.most_common()))

    # Термины глоссария: где встречается термин и не нарушен ли канон.
    # Индекс по первому слову — иначе 108 шаблонов на 462k строк не считаются.
    t_by_first = collections.defaultdict(list)
    for en, ru, bans in terms:
        w = re.findall(r"[A-Za-z]{3,}", en)
        if not w:
            continue
        rx = re.compile(r"(?<![A-Za-z])" + re.escape(en) + r"\w{0,3}(?![A-Za-z])", re.I)
        bad = [re.compile(r"(?<![А-Яа-яЁё])" + re.escape(b.strip()) + r"(?![А-Яа-яЁё])")
               for b in bans.split("/") if len(b.strip()) > 3]
        # Канон ищем по ОСНОВЕ: «Сила» в тексте стоит «силы», «силу», «силой».
        # Пояснения в скобках («Инквест (склоняется: …)») отрезаем.
        head = re.split(r"[(/;]", ru)[0].strip()
        words = [x for x in re.findall(r"[А-Яа-яЁё]{4,}", head)]
        canon = re.compile("|".join(re.escape(x[:max(4, len(x) - 2)])
                                    for x in words), re.I) if words else None
        t_by_first[w[0].lower()].append((en, rx, bad, canon))
    hits = []
    for h, (en, ru) in strings.items():
        if not en:
            continue
        for w in {x.lower() for x in re.findall(r"[A-Za-z]{3,}", en)}:
            for tname, rx, bad, canon in t_by_first.get(w, ()):
                if not rx.search(en):
                    continue
                if not ru:
                    continue
                if any(b.search(ru) for b in bad):
                    mark = "запрещённая форма"
                elif (canon is not None and not canon.search(ru)
                      and ctx_of.get(hx(h)) == "механика"):
                    # только в механике: в художественном тексте канон не указ
                    mark = "канона нет в переводе"
                else:
                    mark = ""
                hits.append((hx(h), tname, mark))
    db.executemany("INSERT INTO term_hit VALUES (?,?,?)", hits)
    nban = sum(1 for x in hits if x[2] == "запрещённая форма")
    nmiss = sum(1 for x in hits if x[2] == "канона нет в переводе")
    print("попаданий терминов: %d | запрещённых форм: %d | без канона в переводе: %d"
          % (len(hits), nban, nmiss))

    # Умения и таланты с привязкой к профессии, типу и оружию: так видно
    # покрытие в разрезах, а не общим числом строк (tools/apicat.py skills).
    ru_by_en = {}
    for h, (en, ru) in strings.items():
        if en and ru:
            ru_by_en.setdefault(en.strip(), ru)
    sk = []
    sfp = os.path.join(CROWD, "sync", "api", "skills.csv")
    if os.path.exists(sfp):
        for r in list(csv.reader(open(sfp, encoding="utf-8-sig")))[1:]:
            if len(r) < 9:
                continue
            vid, sid, nm, ds, prof, typ, wpn, slot = r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]
            sk.append((sid, vid, nm, ds, prof, typ, wpn, slot,
                       ru_by_en.get(nm, ""), ru_by_en.get(ds, "")))
    db.executemany("INSERT INTO skill VALUES (?,?,?,?,?,?,?,?,?,?)", sk)
    have_n = sum(1 for x in sk if x[8])
    have_d = sum(1 for x in sk if x[3] and x[9])
    withd = sum(1 for x in sk if x[3])
    print("умений и талантов: %d | название у нас есть: %d | описание: %d из %d"
          % (len(sk), have_n, have_d, withd))

    # Тип сущности: чем эта штука является по данным API (tools/apicat.py)
    apid = os.path.join(CROWD, "sync", "api")
    types = {}
    if os.path.isdir(apid):
        for fn in os.listdir(apid):
            if not fn.endswith(".csv") or fn in ("charr_wiki.csv", "warband_roster.csv"):
                continue
            for r in list(csv.reader(open(os.path.join(apid, fn), encoding="utf-8-sig")))[1:]:
                if len(r) >= 2 and r[0].strip():
                    types.setdefault(r[0].strip(), r[1].strip())
    db.execute("ALTER TABLE entity ADD COLUMN type TEXT")
    db.executemany("UPDATE entity SET type=? WHERE en=?",
                   ((t, e) for e, t in types.items()))
    n = db.execute("SELECT count(*) FROM entity WHERE type IS NOT NULL").fetchone()[0]
    print("сущностей с типом из API: %d" % n)

    db.commit()
    print("-> %s" % os.path.relpath(DB, CROWD))


def cmd_find(args):
    if not args:
        sys.exit("что искать?")
    q = " ".join(args)
    db = connect()
    rows = db.execute(
        "SELECT hash, english, ru FROM string WHERE english LIKE ? LIMIT 12",
        ("%" + q + "%",)).fetchall()
    if not rows:
        print("не найдено")
        return
    for h, en, ru in rows:
        print("\n%s\n  RU %s" % (en[:150], (ru or "(нет перевода)")[:150]))
        for kind, name, ref in db.execute(
                "SELECT kind, name, ref FROM place WHERE hash=? ORDER BY kind", (h,)):
            print("     %-10s %-26s %s" % (kind, name, ref))


def cmd_who(args):
    if not args:
        sys.exit("про кого?")
    q = " ".join(args)
    db = connect()
    row = db.execute("SELECT en, ru, layer FROM entity WHERE en LIKE ? "
                     "ORDER BY length(en) LIMIT 1", ("%" + q + "%",)).fetchone()
    if not row:
        print("в слое имён нет: %s" % q)
        return
    en, ru, layer = row
    n = db.execute("SELECT count(*) FROM mention WHERE en=?", (en,)).fetchone()[0]
    print("%s -> %s\n  слой: %s | упоминаний в строках: %d" % (en, ru, layer, n))

    w = db.execute("SELECT wb, legion, charr FROM warband WHERE en=?", (en,)).fetchone()
    if w:
        bits = []
        if w[2]:
            bits.append("чарр")
        if w[1]:
            bits.append("%s Legion" % w[1])
        if w[0]:
            bits.append("отряд %s" % w[0])
        print("  вики: %s" % ", ".join(bits))
        if w[0]:
            mates = [r[0] for r in db.execute(
                "SELECT en FROM warband WHERE wb=? AND en<>?", (w[0], en))]
            if mates:
                print("  однополчане:")
                for m in mates:
                    mru = db.execute("SELECT ru FROM entity WHERE en=?", (m,)).fetchone()
                    print("     %-28s %s" % (m, mru[0] if mru else ""))

    print("\n  где встречается (по категориям словаря):")
    for name, c in db.execute(
            "SELECT p.name, count(DISTINCT p.hash) FROM mention m JOIN place p "
            "ON p.hash=m.hash WHERE m.en=? AND p.kind='категория' "
            "GROUP BY p.name ORDER BY 2 DESC LIMIT 10", (en,)):
        print("     %5d  %s" % (c, name))

    # соседи: имена, которые чаще всего встречаются с этим в одних строках
    print("\n  чаще всего рядом:")
    for other, c in db.execute(
            "SELECT m2.en, count(*) FROM mention m1 JOIN mention m2 "
            "ON m1.hash=m2.hash AND m2.en<>m1.en WHERE m1.en=? "
            "GROUP BY m2.en ORDER BY 2 DESC LIMIT 8", (en,)):
        oru = db.execute("SELECT ru FROM entity WHERE en=?", (other,)).fetchone()
        print("     %4d  %-28s %s" % (c, other, oru[0] if oru else ""))

    print("\n  примеры строк:")
    for e, r in db.execute(
            "SELECT s.english, s.ru FROM mention m JOIN string s ON s.hash=m.hash "
            "WHERE m.en=? AND length(s.english)>40 LIMIT 3", (en,)):
        print("     EN %s\n     RU %s" % (e[:110], (r or "")[:110]))


def cmd_term(args):
    """Где встречается термин глоссария и не нарушен ли канон."""
    db = connect()
    if not args:
        print("%-34s %-26s %7s %8s" % ("EN", "канон", "строк", "нарушений"))
        for en, ru, n, bad in db.execute(
                "SELECT t.en, t.ru, count(h.hash), "
                "sum(CASE WHEN h.bad<>'' THEN 1 ELSE 0 END) "
                "FROM term t LEFT JOIN term_hit h ON h.term=t.en "
                "GROUP BY t.en ORDER BY 3 DESC LIMIT 40"):
            print("%-34s %-26s %7d %8d" % (en[:34], (ru or "")[:26], n, bad or 0))
        return
    q = " ".join(args)
    row = db.execute("SELECT en, ru, banned FROM term WHERE en LIKE ? LIMIT 1",
                     ("%" + q + "%",)).fetchone()
    if not row:
        print("нет такого термина в GLOSSARY.md")
        return
    en, ru, banned = row
    n = db.execute("SELECT count(*) FROM term_hit WHERE term=?", (en,)).fetchone()[0]
    print("%s -> %s\n  строк с термином: %d\n  запрещено: %s"
          % (en, ru, n, banned or "—"))
    print("\n  по категориям:")
    for name, c in db.execute(
            "SELECT p.name, count(DISTINCT p.hash) FROM term_hit t "
            "JOIN place p ON p.hash=t.hash WHERE t.term=? AND p.kind='категория' "
            "GROUP BY p.name ORDER BY 2 DESC LIMIT 8", (en,)):
        print("     %6d  %s" % (c, name))
    bad = db.execute("SELECT s.english, s.ru FROM term_hit t JOIN string s "
                     "ON s.hash=t.hash WHERE t.term=? AND t.bad<>'' LIMIT 5",
                     (en,)).fetchall()
    if bad:
        print("\n  НАРУШЕНИЯ канона:")
        for e, r in bad:
            print("     EN %s\n     RU %s" % (e[:100], (r or "")[:100]))


def cmd_bad(args):
    """Дефекты: сводка, либо по категории, либо по имени персонажа."""
    db = connect()
    if not args:
        print("%-34s %7s" % ("класс", "строк"))
        for kind, c in db.execute(
                "SELECT kind, count(DISTINCT hash) FROM defect "
                "GROUP BY kind ORDER BY 2 DESC LIMIT 25"):
            print("%-34s %7d" % (kind[:34], c))
        return
    q = " ".join(args)
    rows = db.execute(
        "SELECT d.kind, count(DISTINCT d.hash) FROM defect d JOIN mention m "
        "ON m.hash=d.hash WHERE m.en LIKE ? GROUP BY d.kind ORDER BY 2 DESC",
        ("%" + q + "%",)).fetchall()
    if rows:
        print("дефекты в строках, где упомянут «%s»:" % q)
        for kind, c in rows:
            print("   %5d  %s" % (c, kind[:60]))
    rows = db.execute(
        "SELECT d.kind, count(DISTINCT d.hash) FROM defect d JOIN place p "
        "ON p.hash=d.hash WHERE p.kind='категория' AND p.name LIKE ? "
        "GROUP BY d.kind ORDER BY 2 DESC LIMIT 15", ("%" + q + "%",)).fetchall()
    if rows:
        print("\nдефекты в категории «%s»:" % q)
        for kind, c in rows:
            print("   %5d  %s" % (c, kind[:60]))


def cmd_skills(args):
    """Покрытие умений: что забрали, а чего нет — по профессии, типу, оружию."""
    db = connect()
    col = {"проф": "prof", "тип": "type", "оружие": "weapon", "слот": "slot"}
    key = col.get(args[0] if args else "проф", "prof")
    print("%-22s %6s %8s %8s %9s" % ("разрез", "всего", "имя", "описание", "нет имени"))
    for v, n, nm, ds, wd in db.execute(
            "SELECT CASE WHEN %s='' THEN '(нет)' ELSE %s END, count(*), "
            "sum(CASE WHEN name_ru<>'' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN descr<>'' AND descr_ru<>'' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN descr<>'' THEN 1 ELSE 0 END) "
            "FROM skill GROUP BY 1 ORDER BY 2 DESC" % (key, key)):
        gap = n - nm
        print("%-22s %6d %8d %8s %9d"
              % (v[:22], n, nm, "%d/%d" % (ds, wd), gap))
    miss = db.execute("SELECT name, prof, type, weapon FROM skill "
                      "WHERE name_ru='' LIMIT 12").fetchall()
    if miss:
        print("\nбез перевода названия, примеры:")
        for nm, p, t, w in miss:
            print("   %-42s %s %s %s" % (nm[:42], p, t, w))


def cmd_cat(args):
    db = connect()
    if not args:
        print("%-30s %8s" % ("категория", "записей"))
        for name, c in db.execute(
                "SELECT name, count(*) FROM place WHERE kind='категория' "
                "GROUP BY name ORDER BY 2 DESC"):
            print("%-30s %8d" % (name, c))
        return
    q = " ".join(args)
    for e, r in db.execute(
            "SELECT s.english, s.ru FROM place p JOIN string s ON s.hash=p.hash "
            "WHERE p.kind='категория' AND p.name LIKE ? LIMIT 15", ("%" + q + "%",)):
        print("  %-70s %s" % (e[:70], (r or "")[:60]))


def cmd_dup(_args):
    db = connect()
    rows = db.execute(
        "SELECT hash, count(DISTINCT name) c FROM place WHERE kind='категория' "
        "GROUP BY hash HAVING c>1").fetchall()
    print("записей больше чем в одной категории: %d" % len(rows))
    shown = 0
    for h, _c in rows:
        cats = [r[0] for r in db.execute(
            "SELECT name FROM place WHERE hash=? AND kind='категория'", (h,))]
        en = db.execute("SELECT english FROM string WHERE hash=?", (h,)).fetchone()[0]
        if shown < 10:
            print("  %-58s %s" % ((en or "")[:58], " + ".join(cats)))
            shown += 1


CMDS = {"build": cmd_build, "find": cmd_find, "who": cmd_who,
        "cat": cmd_cat, "dup": cmd_dup, "term": cmd_term, "bad": cmd_bad, "skills": cmd_skills}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
