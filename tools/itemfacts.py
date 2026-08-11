#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Что это за предмет: тип, слот, вес брони, редкость — от игры, а не на глаз.

    python tools/itemfacts.py fetch          # выгрузить /v2/items и /v2/skins в sync/api/facts/
    python tools/itemfacts.py show <текст>   # чем является предмет с таким названием
    python tools/itemfacts.py batch <файл>   # разложить батч по типам предметов

Зачем. В названиях предметов тип вещи спрятан за словом сета: «Aureate Targe» —
это щит, «Aureate Virge» — скипетр, «Aureate Dirk» — кинжал, «Assaulter's Greatbow»
— длинный лук, а «Amice» и «Spaulders» — оба наплечники, только лёгкие и тяжёлые.
Переводчик, который этого не знает, пишет «тарг», «вирга» и «ринблейд» — что и
случилось в машинных батчах. Игра сама знает ответ: `/v2/items` отдаёт
`type` (Weapon/Armor/Trinket) и `details.type` (Shield/Scepter/Dagger), а для брони
ещё `details.weight_class` (Light/Medium/Heavy). Этого хватает, чтобы выбрать
русское существительное и не спутать наплечники со шлемом.

Выгрузка ложится в sync/api/facts/, а не в sync/api/ рядом с остальными: файлы
верхнего уровня `apicat.py` и `index.py` читают как «название -> категория
словаря», и таблица с колонкой «Shield» во второй графе им не по адресу.
Подхватывается графом (`tools/index.py build` -> таблица `item`).
"""
import csv, json, os, re, sys, time, urllib.request, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
OUTDIR = os.path.join(CROWD, "sync", "api", "facts")
ITEMS = os.path.join(OUTDIR, "items.csv")
API = "https://api.guildwars2.com/v2/"

HEAD = ["name", "kind", "type", "subtype", "weight", "rarity", "level"]


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as f:
                return json.load(f)
        except Exception as e:
            if i == tries - 1:
                print("  ! %s: %s" % (url[-70:], e))
                return None
            time.sleep(2)


def rows_of(o, kind):
    """Одна строка фактов на предмет. Описание не берём — оно у графа уже есть."""
    nm = (o.get("name") or "").strip()
    if not nm:
        return None
    d = o.get("details") or {}
    return (nm, kind, o.get("type") or "", d.get("type") or "",
            d.get("weight_class") or "", o.get("rarity") or "",
            str(o.get("level") or ""))


def cmd_fetch(args):
    os.makedirs(OUTDIR, exist_ok=True)
    want = args or ["items", "skins"]
    seen = set()
    if os.path.exists(ITEMS):
        # докачка: если выгрузка уже есть, старые строки не теряем
        for r in list(csv.reader(open(ITEMS, encoding="utf-8")))[1:]:
            if len(r) == len(HEAD):
                seen.add(tuple(r))
    for kind in want:
        ids = get(API + kind)
        if not isinstance(ids, list):
            print("%s: список id не пришёл" % kind)
            continue
        got, t0 = 0, time.time()
        for i in range(0, len(ids), 200):
            chunk = ",".join(str(x) for x in ids[i:i + 200])
            data = get("%s%s?ids=%s&lang=en" % (API, kind, chunk))
            if not isinstance(data, list):
                continue
            for o in data:
                if isinstance(o, dict):
                    r = rows_of(o, kind)
                    if r:
                        seen.add(r)
                        got += 1
            if (i // 200) % 25 == 0:
                print("  %s %6d/%d  (%.0f c)" % (kind, i, len(ids), time.time() - t0),
                      flush=True)
        print("%-8s id %6d -> фактов %6d" % (kind, len(ids), got))
    with open(ITEMS, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEAD)
        w.writerows(sorted(seen))
    print("-> %s  строк %d" % (os.path.relpath(ITEMS, CROWD), len(seen)))


def load(path=None):
    """name -> список фактов. Имя не уникально: у «Ring of Red Death» есть
    вознесённая и экзотическая версия, у обликов — тёзка-предмет."""
    by = collections.defaultdict(list)
    path = path or ITEMS
    if not os.path.exists(path):
        return by
    for r in list(csv.reader(open(path, encoding="utf-8-sig")))[1:]:
        if len(r) >= len(HEAD) and r[0].strip():
            by[r[0].strip()].append(dict(zip(HEAD, r)))
    return by


def human(f):
    bits = [f["type"]]
    if f["subtype"] and f["subtype"] != f["type"]:
        bits.append(f["subtype"])
    if f["weight"]:
        bits.append(f["weight"])
    s = "/".join(x for x in bits if x)
    if f["rarity"]:
        s += " · " + f["rarity"]
    return s


def cmd_show(args):
    if not args:
        sys.exit("что искать?")
    q = " ".join(args).lower()
    by = load()
    if not by:
        sys.exit("нет выгрузки — сначала `itemfacts.py fetch`")
    n = 0
    for nm in sorted(by):
        if q in nm.lower():
            for f in by[nm]:
                print("%-52s %s" % (nm[:52], human(f)))
            n += 1
            if n >= 60:
                print("...")
                return
    if not n:
        print("в выгрузке нет: %s" % " ".join(args))


def cmd_batch(args):
    """Разложить батч по типам: видно, какие семьи строк в нём вообще есть."""
    if not args:
        sys.exit("какой батч?")
    fp = args[0]
    if not os.path.exists(fp):
        fp = os.path.join(CROWD, args[0])
    by = load()
    rows = list(csv.reader(open(fp, encoding="utf-8")))[1:]
    cnt = collections.Counter()
    unknown = []
    for r in rows:
        if not r or not r[0].strip():
            continue
        f = by.get(r[0].strip())
        if f:
            cnt[human(f[0])] += 1
        else:
            cnt["— нет в выгрузке"] += 1
            unknown.append(r[0])
    for k, v in cnt.most_common():
        print("%5d  %s" % (v, k))
    if unknown:
        print("\nнет в выгрузке (%d), первые 20:" % len(unknown))
        for u in unknown[:20]:
            print("   %s" % u)


CMDS = {"fetch": cmd_fetch, "show": cmd_show, "batch": cmd_batch}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
