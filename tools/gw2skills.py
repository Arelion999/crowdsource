#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Русские описания умений с ru.gw2skills.net.

    python tools/gw2skills.py fetch    # скачать описания в sync/api/gw2skills.csv
    python tools/gw2skills.py match    # что из этого закрывает наши дыры

ИСТОЧНИК И УСЛОВИЯ. Переводы принадлежат Gw2Skills.Net. Сайт требует: «при
использовании переводов ссылка на данный сайт является обязательной». Ссылка
стоит в README (раздел «Источники») и в GLOSSARY.md. Без неё этим пользоваться
нельзя.

Как соединяется: на русских страницах сайта названия умений оставлены
английскими, а описания переведены. У нас из официального API есть пара
«английское название -> английское описание». Значит по английскому названию
получаем «английское описание -> русское описание» — ровно то, чем заполняются
дыры (описаний нет у 1 355 умений из 5 304).
"""
import csv, html, os, re, sys, time, urllib.request, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
OUT = os.path.join(CROWD, "sync", "api", "gw2skills.csv")
SITE = "https://ru.gw2skills.net"
PROFS = ["elementalist", "engineer", "guardian", "mesmer", "necromancer",
         "ranger", "revenant", "thief", "warrior"]
PAGES = ["skills", "traits", "special"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as f:
                raw = f.read()
            t = raw.decode("utf-8", "replace")
            # страницы отдаются в двойной кодировке: utf-8, прочитанный как cp1251
            if t.count("Р") > 200:
                t = t.encode("cp1251", "ignore").decode("utf-8", "replace")
            return t
        except Exception as e:
            if i == 2:
                print("  ! %s: %s" % (url[-46:], e))
                return ""
            time.sleep(2)


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


EN_NAME = re.compile(r"^[A-Z][A-Za-z0-9'’!\-\"(),.:& ]{2,}$")
CYR = re.compile(r"[А-Яа-яЁё]")


def parse(page):
    """Строка таблицы: английское название, потом числа, потом русское описание."""
    out = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        name = cells[0]
        if not EN_NAME.match(name) or CYR.search(name):
            continue
        descr = next((c for c in reversed(cells)
                      if CYR.search(c) and len(c) > 25), "")
        if not descr:
            continue
        # У них метка типа умения («Украденное умение», «Сдвоенная атака») стоит
        # в той же ячейке и при снятии тегов склеивается с описанием. Признак —
        # пробел ПЕРЕД точкой: «Украденное умение . Бросить слизь…». В игровой
        # строке эта метка живёт отдельно, в <c=@abilitytype>, и наша там своя.
        m = re.match(r"^(.{1,30}?)\s+\.\s+(?=[А-ЯЁ])", descr)
        label = ""
        if m:
            label, descr = m.group(1), descr[m.end():]
        out.append((name, descr, label))
    return out


def cmd_fetch(_a):
    rows, seen = [], set()
    for prof in PROFS:
        for page in PAGES:
            url = "%s/wiki/%s/%s/" % (SITE, prof, page)
            t = get(url)
            if not t:
                continue
            got = parse(t)
            new = 0
            for nm, ds, lab in got:
                if (nm, ds) in seen:
                    continue
                seen.add((nm, ds))
                rows.append((nm, ds, lab, prof, page))
                new += 1
            print("  %-14s %-8s строк %4d (новых %d)" % (prof, page, len(got), new))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "описание_ru", "метка", "профессия", "раздел"])
        w.writerows(rows)
    print("\nописаний: %d | уникальных названий: %d -> %s"
          % (len(rows), len({r[0] for r in rows}), os.path.relpath(OUT, CROWD)))
    print("Источник: %s — при использовании ссылка обязательна." % SITE)


def cmd_match(_a):
    import sqlite3
    db = sqlite3.connect(os.path.join(CROWD, "sync", "index.db"))
    if not os.path.exists(OUT):
        sys.exit("нет выгрузки — сначала `gw2skills.py fetch`")
    theirs = {}
    for r in list(csv.reader(open(OUT, encoding="utf-8-sig")))[1:]:
        if len(r) >= 2:
            theirs.setdefault(r[0], r[1])
    rows = db.execute("SELECT name, descr, descr_ru, prof FROM skill "
                      "WHERE descr<>''").fetchall()
    gap = [r for r in rows if not r[2]]
    can = [r for r in gap if r[0] in theirs]
    print("умений с описанием в API: %d | у нас нет перевода: %d"
          % (len(rows), len(gap)))
    print("из них есть у gw2skills: %d" % len(can))
    per = collections.Counter(r[3] or "(общие)" for r in can)
    for p, n in per.most_common(10):
        print("   %5d  %s" % (n, p))
    print("\nпримеры:")
    for nm, ds, _ru, _p in can[:5]:
        print("  %s\n    EN %s\n    RU %s" % (nm, ds[:95], theirs[nm][:95]))


CMDS = {"fetch": cmd_fetch, "match": cmd_match}
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
