#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Категории строк по официальному API — авторитет вместо догадок.

    python tools/apicat.py fetch [<тип> ...]   # скачать названия по типам в sync/api/
    python tools/apicat.py check               # где лежит не в своей категории
    python tools/apicat.py apply               # переложить (с бэкапом и гейтом)

Зачем. Категория в словаре — это группа отключения: игрок гасит «Предметы:
названия» и видит английские названия для вики и торговли. Категории пришли
из исходных `dict_*.csv`, а `sync.py` всё, что прокси выучил позже, дописывает
в `main_strings.csv`, то есть в «основной». Поэтому «основной» распух до 188 710
записей.

Замер (2026-08-11) показал, что раскладка при этом почти цела: из 4 566 названий,
которые отдаёт API, 3 168 уже лежат в своей категории, 1 352 — в третьей (чаще
всего законно: имена квестов живут в «Личной истории», и это их место), и лишь
**30** в «основном» мимо профильного словаря. Расхождение «в API 586 квестов, у
нас в `quests` 31» само по себе НЕ означает беспорядка — остальные разложены по
сюжетным категориям.

Набор эндпоинтов API почти совпадает с нашими категориями — значит и восстанавливать
раскладку надо оттуда же, а не по смыслу строки.
"""
import csv, json, os, re, sys, time, urllib.request, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
APIDIR = os.path.join(CROWD, "sync", "api")
sys.path.insert(0, HERE)
import dict_tool as D

# наша категория -> эндпоинт API. Только те, где название однозначно:
# items/skins не берём, они огромны и требуют отдельного захода.
TYPES = {
    "quests": "quests",
    "titles": "titles",
    "masteries": "masteries",
    "currencies": "currencies",
    "novelties": "novelties",
    "finishers": "finishers",
    "mailcarriers": "mailcarriers",
    "skiffs": "skiffs",
    "jadebots": "jadebots",
    "outfits": "outfits",
    "colors": "colors",
    "minis": "minis",
    "gliders": "gliders",
    "pets": "pets",
    "specializations": "specializations",
    "professions": "professions",
    "maps": "maps",
    "achievement_categories": "achievements/categories",
    "achievement_groups": "achievements/groups",
    "guild_permissions": "guild/permissions",
    "wvw_abilities": "wvw/abilities",
    "wvw_objectives": "wvw/objectives",
    "wvw_ranks": "wvw/ranks",
    "pvp_ranks": "pvp/ranks",
    "pvp_heroes": "pvp/heroes",
}
API = "https://api.guildwars2.com/v2/"


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as f:
                return json.load(f)
        except Exception as e:
            if i == tries - 1:
                print("  ! %s: %s" % (url[-60:], e))
                return None
            time.sleep(2)


def names_of(obj):
    """Только НАЗВАНИЕ. Описание брать нельзя: «This glider is dyeable.» и прочие
    общие тексты повторяются у десятков предметов, и по ним строку припишет не
    туда."""
    out = []
    for k in ("name", "title"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def cmd_fetch(args):
    os.makedirs(APIDIR, exist_ok=True)
    want = args or list(TYPES)
    for cat in want:
        ep = TYPES.get(cat)
        if not ep:
            print("не знаю тип: %s" % cat)
            continue
        ids = get(API + ep)
        if not isinstance(ids, list):
            continue
        rows = []
        for i in range(0, len(ids), 200):
            chunk = ",".join(str(x) for x in ids[i:i + 200])
            data = get("%s%s?ids=%s&lang=en" % (API, ep, chunk))
            if not isinstance(data, list):
                continue
            for o in data:
                if isinstance(o, dict):
                    for nm in names_of(o):
                        rows.append((nm, cat))
        fp = os.path.join(APIDIR, cat + ".csv")
        with open(fp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            # колонку НЕ звать «english»: validate.py ищет батчи рекурсивно и
            # по такому заголовку считал выгрузки API за батчи (+5 055 строк)
            w.writerow(["name", "category"])
            w.writerows(sorted(set(rows)))
        print("%-24s ids %5d -> строк %5d" % (cat, len(ids), len(set(rows))))


def load_api():
    want = {}
    for fp in sorted(os.listdir(APIDIR)) if os.path.isdir(APIDIR) else []:
        if not fp.endswith(".csv"):
            continue
        for r in list(csv.reader(open(os.path.join(APIDIR, fp), encoding="utf-8")))[1:]:
            if len(r) >= 2 and r[0].strip():
                want.setdefault(r[0].strip(), r[1].strip())
    return want


def survey():
    want = load_api()
    if not want:
        sys.exit("нет выгрузки API — сначала `apicat.py fetch`")
    where = collections.defaultdict(set)
    en_of = {}
    for name, es in D.read_sections(D.OUR_BIN):
        cat = name.partition("\x1f")[0]
        for h, en, _ru in es:
            where[h].add(cat)
            if en:
                en_of[h] = en
    move, ok, absent = [], 0, 0
    for h, cats in where.items():
        en = en_of.get(h)
        cat = want.get(en) if en else None
        if not cat:
            continue
        if cat in cats:
            ok += 1
        elif cats <= {"основной", "выученные"}:
            move.append((h, en, cat, sorted(cats)))
        else:
            absent += 1
    return want, move, ok, absent


def cmd_check(_args):
    want, move, ok, absent = survey()
    print("названий из API: %d | уже в своей категории: %d" % (len(want), ok))
    print("лежат в «основной»/«выученные», а место в профильном: %d" % len(move))
    print("лежат в третьей категории (не трогаем): %d" % absent)
    per = collections.Counter(c for _h, _en, c, _f in move)
    print("\nчто и куда переложить:")
    for c, n in per.most_common():
        print("   %6d  -> %s" % (n, c))
    print("\nпримеры:")
    for _h, en, c, frm in move[:12]:
        print("   %-58s %s -> %s" % (en[:58], "+".join(frm), c))


def cmd_apply(_args):
    _want, move, _ok, _absent = survey()
    if not move:
        print("нечего перекладывать")
        return
    target = {h: c for h, _en, c, _f in move}
    sections = D.read_sections(D.OUR_BIN)
    by_cat = {}
    order = []
    for name, es in sections:
        by_cat[name] = list(es)
        order.append(name)
    short = {n.partition("\x1f")[0]: n for n in order}
    moved = 0
    for name in order:
        keep = []
        for h, en, ru in by_cat[name]:
            cat = name.partition("\x1f")[0]
            dst = target.get(h)
            if dst and cat in ("основной", "выученные") and dst != cat:
                dname = short.get(dst)
                if dname:
                    by_cat[dname].append((h, en, ru))
                    moved += 1
                    continue
            keep.append((h, en, ru))
        by_cat[name] = keep
    bak = D.backup(D.OUR_BIN)
    ncat, total = D.write_bin(D.OUR_BIN, [(n, by_cat[n]) for n in order])
    print("переложено: %d | в bin %d категорий, %d записей" % (moved, ncat, total))
    print("бэкап: %s" % os.path.relpath(bak, D.ROOT))


MECH = os.path.join(CROWD, "sync", "api", "mechanical.csv")
SKILLS = os.path.join(CROWD, "sync", "api", "skills.csv")


def cmd_skills(_args):
    """Умения и таланты с их принадлежностью: профессия, тип, оружие, слот.

    Нужно, чтобы видеть покрытие не общим числом, а по разрезам: сколько умений
    элементалиста с посохом у нас переведено, а сколько вообще не заведено.
    Текст без этой привязки показывает только «сколько-то строк».
    """
    rows = []
    for ep in ("skills", "traits"):
        ids = get(API + ep)
        if not isinstance(ids, list):
            continue
        for i in range(0, len(ids), 200):
            chunk = ",".join(str(x) for x in ids[i:i + 200])
            data = get("%s%s?ids=%s&lang=en" % (API, ep, chunk))
            if not isinstance(data, list):
                continue
            for o in data:
                if not isinstance(o, dict):
                    continue
                prof = o.get("professions") or []
                if ep == "traits":
                    prof = [o.get("profession")] if o.get("profession") else []
                rows.append((
                    ep[:-1], str(o.get("id", "")), o.get("name", "") or "",
                    (o.get("description", "") or "").replace("\n", " "),
                    "/".join(str(p) for p in prof),
                    o.get("type", "") or "", o.get("weapon_type", "") or "",
                    o.get("slot", "") or "", str(o.get("specialization", "") or "")))
        print("%-8s ids %5d" % (ep, len(ids)))
    os.makedirs(os.path.dirname(SKILLS), exist_ok=True)
    with open(SKILLS, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["вид", "id", "name", "description", "профессии", "тип",
                    "оружие", "слот", "спец"])
        w.writerows(rows)
    print("умений и талантов: %d -> %s" % (len(rows), os.path.relpath(SKILLS, CROWD)))


def cmd_mech(_args):
    """Механический текст: названия и ОПИСАНИЯ умений и талантов.

    Нужен, чтобы отделить механику от художественного текста. Канон боевых
    терминов («Проворство», «Заморозка») обязателен в описании умения и НЕ
    обязателен в реплике: «the foolish vigor of youth» — это не бон, а обычное
    слово, и «энергия юности» там правильнее «энергичности».

    Границу берём у самой игры: что API отдаёт по /skills и /traits — механика.
    """
    rows = []
    for ep, tag in (("skills", "умение"), ("traits", "талант")):
        ids = get(API + ep)
        if not isinstance(ids, list):
            continue
        for i in range(0, len(ids), 200):
            chunk = ",".join(str(x) for x in ids[i:i + 200])
            data = get("%s%s?ids=%s&lang=en" % (API, ep, chunk))
            if not isinstance(data, list):
                continue
            for o in data:
                if not isinstance(o, dict):
                    continue
                for k in ("name", "description"):
                    v = o.get(k)
                    if isinstance(v, str) and v.strip():
                        rows.append((v.strip(), tag, k))
                for f in (o.get("facts") or []):
                    for k in ("text", "status", "description"):
                        v = f.get(k) if isinstance(f, dict) else None
                        if isinstance(v, str) and v.strip():
                            rows.append((v.strip(), tag, "факт"))
        print("%-8s ids %5d" % (ep, len(ids)))
    os.makedirs(os.path.dirname(MECH), exist_ok=True)
    with open(MECH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "вид", "поле"])
        w.writerows(sorted(set(rows)))
    print("строк механического текста: %d -> %s"
          % (len(set(rows)), os.path.relpath(MECH, CROWD)))


CMDS = {"fetch": cmd_fetch, "check": cmd_check, "apply": cmd_apply,
        "mech": cmd_mech, "skills": cmd_skills}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
