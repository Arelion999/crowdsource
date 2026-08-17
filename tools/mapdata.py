#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Всё, что привязано к месту на карте: континент, регион, карта, сектор, точка.

    python tools/mapdata.py fetch     # выгрузить в sync/api/places.csv
    python tools/mapdata.py show <текст>   # где это на карте
    python tools/mapdata.py stat      # что выгружено

Зачем. Половина корпуса — это названия мест и то, что к местам привязано:
путевые точки, достопримечательности, обзорные площадки, сердца почёта,
испытания героя. Переводчик видит строку «Fridgardr Lodge» и не знает ни того,
что это, ни где оно; а от места зависит и род («Убежище» или «Застава»), и
соседи по канону — постройки одного региона должны звучать одинаково.

Игра отдаёт это деревом: /v2/continents/<c>/floors/<f> -> regions -> maps ->
points_of_interest | sectors | tasks | skill_challenges | mastery_points |
adventures. Этажи — это варианты одной и той же местности (разные уровни,
сюжетные версии), поэтому обходим все и схлопываем по (тип, id), иначе часть
карт вида «инстанс» не встретится вовсе.

Результат ложится в sync/api/places.csv и подхватывается графом
(`tools/index.py build` -> таблица geo).
"""
import csv, json, os, sys, time, urllib.request, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
OUT = os.path.join(CROWD, "sync", "api", "places.csv")
API = "https://api.guildwars2.com/v2"
HEAD = ["name", "kind", "map", "region", "continent", "x", "y", "id"]


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=90) as f:
                return json.load(f)
        except Exception as e:
            if i == tries - 1:
                print("  ! %s: %s" % (url[-70:], e))
                return None
            time.sleep(2)


def cmd_fetch(_args):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    conts = get("%s/continents?ids=all&lang=en" % API) or []
    seen, rows = set(), []

    def add(name, kind, mp, reg, cont, coord, ident):
        name = (name or "").strip()
        if not name:
            return
        k = (kind, str(ident), name)
        if k in seen:
            return
        seen.add(k)
        x, y = (coord or ["", ""])[:2]
        rows.append((name, kind, mp, reg, cont, x, y, ident))

    for c in conts:
        cname, floors = c["name"], c["floors"]
        print("континент %s: этажей %d" % (cname, len(floors)), flush=True)
        for n, fl in enumerate(floors, 1):
            d = get("%s/continents/%s/floors/%s?lang=en" % (API, c["id"], fl))
            if not d:
                continue
            for reg in (d.get("regions") or {}).values():
                rname = reg.get("name", "")
                add(rname, "region", "", "", cname, reg.get("label_coord"), reg.get("id", ""))
                for mp in (reg.get("maps") or {}).values():
                    mname = mp.get("name", "")
                    add(mname, "map", "", rname, cname, mp.get("label_coord"), mp.get("id", ""))
                    for p in (mp.get("points_of_interest") or {}).values():
                        add(p.get("name"), p.get("type", "poi"), mname, rname, cname,
                            p.get("coord"), p.get("id", ""))
                    for s in (mp.get("sectors") or {}).values():
                        add(s.get("name"), "sector", mname, rname, cname,
                            s.get("coord"), s.get("id", ""))
                    for t in (mp.get("tasks") or {}).values():
                        add(t.get("objective"), "task", mname, rname, cname,
                            t.get("coord"), t.get("id", ""))
                    # испытания героя и точки мастерства пропускаем: у них нет
                    # имени — API отдаёт идентификатор («0-1-1») и название
                    # региона, привязывать к строкам словаря нечего
                    for ad in (mp.get("adventures") or []):
                        add(ad.get("name"), "adventure", mname, rname, cname,
                            ad.get("coord"), ad.get("id", ""))
            if n % 10 == 0:
                print("   этаж %d/%d, мест: %d" % (n, len(floors), len(rows)), flush=True)

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEAD)
        w.writerows(sorted(rows))
    print("-> %s | мест: %d" % (os.path.relpath(OUT, CROWD), len(rows)))
    cnt = collections.Counter(r[1] for r in rows)
    print("по типам: " + " | ".join("%s %d" % kv for kv in cnt.most_common()))


def load():
    by = collections.defaultdict(list)
    if not os.path.exists(OUT):
        return by
    for r in list(csv.reader(open(OUT, encoding="utf-8-sig")))[1:]:
        if len(r) >= len(HEAD) and r[0].strip():
            by[r[0].strip()].append(dict(zip(HEAD, r)))
    return by


KIND_RU = {"region": "регион", "map": "карта", "sector": "сектор", "landmark": "достопримечательность",
           "waypoint": "путевая точка", "vista": "обзор", "unlock": "проход",
           "task": "сердце почёта", "skill_challenge": "испытание героя",
           "mastery_point": "точка мастерства", "adventure": "приключение", "poi": "точка"}


def cmd_show(args):
    if not args:
        sys.exit("что искать?")
    q = " ".join(args).lower()
    by = load()
    if not by:
        sys.exit("нет выгрузки — сначала `mapdata.py fetch`")
    n = 0
    for nm in sorted(by):
        if q in nm.lower():
            for p in by[nm][:3]:
                where = " / ".join(x for x in (p["continent"], p["region"], p["map"]) if x)
                print("%-40s %-22s %s" % (nm[:40], KIND_RU.get(p["kind"], p["kind"]), where))
            n += 1
            if n >= 40:
                print("...")
                return
    if not n:
        print("на карте не найдено: %s" % " ".join(args))


def cmd_stat(_args):
    by = load()
    cnt = collections.Counter(p["kind"] for v in by.values() for p in v)
    print("уникальных названий: %d" % len(by))
    for k, v in cnt.most_common():
        print("   %-18s %6d" % (KIND_RU.get(k, k), v))


CMDS = {"fetch": cmd_fetch, "show": cmd_show, "stat": cmd_stat}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
