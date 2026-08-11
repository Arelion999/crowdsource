#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отряд и легион чарра — по официальной вики.

    python tools/wikilore.py fetch          # спросить вики про всех чарров слоя
    python tools/wikilore.py check          # где перевод не держит корень отряда

Зачем. Фамилия чарра — это имя его отряда, общее у всех членов, и по одному
только виду фамилии состав отряда не собрать: Ритлок Брим**стоун** и Кресия
**Стоун**глоу оба из отряда Stone. Значит связь надо брать из лора, а не гадать
по буквам, — вики её знает и отдаёт машинно.

Результат ложится в sync/api/charr_wiki.csv и подхватывается графом
(`tools/index.py build` -> таблица `warband`).
"""
import csv, json, os, re, sys, time, urllib.parse, urllib.request, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
OUT = os.path.join(CROWD, "sync", "api", "charr_wiki.csv")
sys.path.insert(0, HERE)
import dict_tool as D

WIKI = "https://wiki.guildwars2.com/api.php"
# «member of the Stone Warband», «Farzan Steelshaper's Iron Legion warband»
RE_WB = re.compile(r"(?:member of (?:the )?|of )([A-Z][A-Za-z']+(?: [A-Z][a-z]+)?)"
                   r"(?:'s)?(?: [A-Z][a-z]+ Legion)? [Ww]arband")
RE_WB2 = re.compile(r"\b([A-Z][A-Za-z']+) Warband\b")
RE_LEG = re.compile(r"\b(Blood|Iron|Ash|Flame|Frost) Legion\b")


def api(params):
    url = WIKI + "?" + urllib.parse.urlencode(params)
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=40) as f:
                return json.load(f)
        except Exception as e:
            if i == 2:
                print("  ! %s" % e)
                return None
            time.sleep(2)


def charr_names():
    """Чарры, подтверждённые ручным разбором, плюс их переводы из слоя.

    Брать все двусловные имена слоя (5 076) бессмысленно: чарров среди них
    меньше сотни, а вики на остальные отвечает пустотой.
    """
    ru_of = {}
    for name, es in D.read_sections(D.OUR_BIN):
        if not name.partition("\x1f")[0].startswith("pn_"):
            continue
        for _h, en, ru in es:
            if en and ru:
                ru_of[en.strip()] = ru.strip()
    names = []
    fp = os.path.join(CROWD, "sync", "reports", "charr_clean.csv")
    if os.path.exists(fp):
        for r in list(csv.reader(open(fp, encoding="utf-8-sig")))[1:]:
            if len(r) >= 3 and r[0] == "чарр":
                names.append((r[2], ru_of.get(r[2], "")))
    for extra in ("Kymber Steelsnap", "Farzan Steelshaper", "Crecia Stoneglow",
                  "Kress Rustmaw", "Groma Spinebreaker", "Lutha Oreseeker",
                  "Micka Thickblood", "Kyranith Steelgrip", "Malice Swordshadow",
                  "Smodur the Unflinching", "Almorra Soulkeeper"):
        names.append((extra, ru_of.get(extra, "")))
    return sorted(set(names))


def cmd_fetch(_args):
    names = charr_names()
    print("кандидатов из слоя: %d" % len(names))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    rows, hit = [], 0
    for i in range(0, len(names), 20):
        chunk = names[i:i + 20]
        # exintro обязателен: с полным extract вики отдаёт ОДНУ страницу за
        # запрос, сколько ни проси. Вступление и так содержит отряд и легион.
        d = api({"action": "query", "prop": "extracts", "explaintext": 1,
                 "exintro": 1, "exlimit": "max", "format": "json",
                 "titles": "|".join(n for n, _ru in chunk)})
        if not d or "query" not in d:
            continue
        got = {}
        for p in d["query"]["pages"].values():
            if "extract" in p:
                got[p["title"]] = p["extract"]
        for en, ru in chunk:
            txt = got.get(en, "")
            if not txt:
                continue
            m = RE_WB.search(txt) or RE_WB2.search(txt)
            wb = m.group(1) if m else ""
            leg = RE_LEG.search(txt)
            is_charr = "charr" in txt[:400].lower()
            if wb or is_charr:
                hit += 1
            rows.append((en, ru, wb, leg.group(1) if leg else "",
                         "да" if is_charr else ""))
        print("  %d/%d" % (min(i + 20, len(names)), len(names)), end="\r")
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["english", "перевод", "отряд", "легион", "чарр"])
        w.writerows(rows)
    print("\nстатей нашлось: %d | с отрядом или пометкой «чарр»: %d" % (len(rows), hit))
    print("-> %s" % os.path.relpath(OUT, CROWD))


# корень отряда -> как он должен выглядеть по-русски (из GLOSSARY.md)
ROOT_RU = {
    "Steel": "Стал", "Iron": "Желез", "Blood": "Кров", "Sharp": "Остр",
    "Quick": "Быстр", "Stone": "Камн|Камен", "Crush": "Круш",
    "Anvil": "Наковал", "Burn": "Палён|Пален", "Gore": "Кровав",
    "Scorch": "Жар", "Dark": "Темн|Тёмн", "Ash": "Пепел|Пепл",
}


def cmd_check(_args):
    if not os.path.exists(OUT):
        sys.exit("нет выгрузки — сначала `wikilore.py fetch`")
    rows = list(csv.reader(open(OUT, encoding="utf-8-sig")))[1:]
    by_wb = collections.defaultdict(list)
    for r in rows:
        if len(r) >= 5 and r[2] and r[4] == "да":
            by_wb[r[2]].append((r[0], r[1]))
    print("отрядов с подтверждённым составом: %d" % len(by_wb))
    bad = 0
    for wb, mem in sorted(by_wb.items(), key=lambda x: -len(x[1])):
        rx = ROOT_RU.get(wb)
        miss = [m for m in mem if rx and not re.search(rx, m[1], re.I)]
        mark = ""
        if len(mem) > 1 or miss:
            print("\n-- отряд %s (%d)" % (wb, len(mem)))
            for en, ru in mem:
                ok = "" if (not rx or re.search(rx, ru, re.I)) else "  <- корень потерян"
                if ok:
                    bad += 1
                print("     %-30s %-28s%s" % (en, ru, ok))
    print("\nпереводов без корня своего отряда: %d" % bad)


ROSTER = os.path.join(CROWD, "sync", "api", "warband_roster.csv")


def cmd_rosters(_args):
    """Составы отрядов со страниц вики.

    На странице отряда состав выводится шаблоном `{{member list}}`, то есть
    берётся из категории с тем же именем. Значит и нам надо спрашивать
    `Category:<Отряд>`, а не парсить текст страницы.
    """
    d = api({"action": "query", "list": "categorymembers",
             "cmtitle": "Category:Warbands", "cmlimit": "500", "format": "json"})
    wbs = [x["title"] for x in d.get("query", {}).get("categorymembers", [])
           if x["title"].lower().endswith("warband")
           and not x["title"].startswith("Category:")]
    print("отрядов в категории: %d" % len(wbs))

    ru_of = {}
    for name, es in D.read_sections(D.OUR_BIN):
        if name.partition("\x1f")[0].startswith("pn_"):
            for _h, en, ru in es:
                if en and ru:
                    ru_of[en.strip()] = ru.strip()

    rows = []
    for wb in wbs:
        d = api({"action": "query", "list": "categorymembers",
                 "cmtitle": "Category:" + wb, "cmlimit": "500", "format": "json"})
        mem = [x["title"] for x in d.get("query", {}).get("categorymembers", [])
               if x["title"] != wb and not x["title"].startswith("Category:")]
        if not mem:
            # у части отрядов одноимённой категории нет, и состав перечислен
            # списком в разделе «Members» на самой странице
            p = api({"action": "parse", "page": wb, "prop": "wikitext",
                     "format": "json"})
            txt = (p or {}).get("parse", {}).get("wikitext", {}).get("*", "")
            sec = re.search(r"==+\s*Members?\s*==+(.*?)(?:\n==|\Z)", txt,
                            re.I | re.S)
            if sec:
                mem = re.findall(r"\*\s*\[\[([^\]|]+)", sec.group(1))
        for m in mem:
            rows.append((wb, m, ru_of.get(m, "")))
        print("  %-26s %d" % (wb, len(mem)))
    os.makedirs(os.path.dirname(ROSTER), exist_ok=True)
    with open(ROSTER, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["отряд", "член", "перевод в слое"])
        w.writerows(rows)
    have = sum(1 for _wb, _m, ru in rows if ru)
    print("\nвсего членов: %d | есть в слое: %d | НЕТ в слое: %d"
          % (len(rows), have, len(rows) - have))
    print("-> %s" % os.path.relpath(ROSTER, CROWD))


CMDS = {"fetch": cmd_fetch, "check": cmd_check, "rosters": cmd_rosters}
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
