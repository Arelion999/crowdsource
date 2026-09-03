#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Маршрут строки: в какой категории bin ей лежать. Версионируется в ROUTES.txt.

    python tools/routes.py build     # пересобрать индекс из bin (разовая операция)
    python tools/routes.py check     # что не сходится между батчами и индексом
    python tools/routes.py show "English string"
    python tools/routes.py set <категория> <файл со списком english>

Зачем. Категория — это группа отключения, и до сих пор она хранилась ТОЛЬКО в
bin. Батч знал текст, но не знал места: `frombatches` смотрел, где строка лежит
сейчас, и клала правку туда же. Значит bin был источником истины о раскладке, а
батчи — нет: пересобрать bin с нуля из репозитория было нечем, новая строка
всегда падала в «основной», а перенос строки в другую группу нельзя было ни
записать в git, ни проверить в PR.

Индекс это чинит: раскладка лежит в репозитории рядом с батчами, `frombatches`
берёт категорию отсюда, а не из bin.

Формат — секции по категориям, внутри отсортированные хеши (по 16 hex):

    [item_names]
    0123456789abcdef

Только хеш: english уже лежит в батче, дублировать его сюда — это +40 МБ.
Хеш тот же, что в bin (`dict_tool.fnv1a_u16`, FNV-1a-64 по UTF-16LE).

Слоя `pn_*` в индексе НЕТ: у него маршрут виден по имени файла батча
(`pn/pn_world_map_003.csv` -> `pn_world_map`), то есть уже версионирован.
"""
import collections, csv, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import dict_tool as D

ROUTES = os.path.join(CROWD, "ROUTES.txt")
FIX = os.path.join(CROWD, "ROUTES_FIX.csv")     # ручные решения, поверх индекса
DEFAULT = "основной"
HEAD = (
    "# Где какая строка лежит в dictionary.bin. Категория = группа отключения.\n"
    "# Собирается `tools/routes.py build`, читается `dict_tool.py frombatches`.\n"
    "# Формат: [категория], далее хеши строк (fnv1a-64 по UTF-16LE, 16 hex).\n"
    "# Строка без записи здесь попадает в «%s». Слой pn_* сюда не пишется:\n"
    "# его маршрут виден по имени файла батча.\n" % DEFAULT
)
SEC = re.compile(r"^\[([^\]]+)\]$")


def load():
    """{hash: категория}. Поверх индекса — ручные решения из ROUTES_FIX.csv."""
    out, cat = {}, None
    if os.path.exists(ROUTES):
        with open(ROUTES, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                m = SEC.match(ln)
                if m:
                    cat = m.group(1)
                elif cat:
                    out[int(ln, 16)] = cat
    for en, cat in load_fix().items():
        out[D.fnv1a_u16(en)] = cat
    return out


def load_fix():
    """{english: категория} — ручные решения. Побеждают индекс."""
    out = {}
    if not os.path.exists(FIX):
        return out
    with open(FIX, encoding="utf-8-sig", newline="") as f:
        for r in csv.reader(f):
            if len(r) >= 2 and r[0].strip() and r[0].strip() != "english":
                out[r[0]] = r[1].strip()
    return out


def save(routes):
    """{hash: категория} -> ROUTES.txt."""
    by = collections.defaultdict(list)
    for h, c in routes.items():
        by[c].append(h)
    with open(ROUTES, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEAD)
        for c in sorted(by):
            f.write("[%s]\n" % c)
            for h in sorted(by[c]):
                f.write("%016x\n" % h)
    return len(routes), len(by)


def from_bin():
    """Раскладка, как она сейчас в bin, + разбор конфликтов.

    Один хеш в двух обычных категориях — брак: любой из двух выключателей по
    отдельности не работает, а мод берёт ту копию, что встретил первой. Держим
    ОДНУ категорию на хеш; из двух побеждает МЕНЬШАЯ, то есть более конкретная
    («Забавы» против «Предметы: названия», «Сердца известности» против
    «События»), а «основной» проигрывает всем как самая общая свалка.
    """
    seen, size = collections.defaultdict(list), collections.Counter()
    for name, es in D.read_sections(D.OUR_BIN):
        c = name.partition("\x1f")[0]
        if c.startswith("pn_"):
            continue
        size[c] += len(es)
        for h, en, _ru in es:
            seen[h].append((c, en))
    routes, conflicts = {}, []
    for h, v in seen.items():
        if len(v) == 1:
            routes[h] = v[0][0]
            continue
        cats = sorted({c for c, _ in v}, key=lambda c: (size[c], c))
        routes[h] = cats[0]
        conflicts.append((v[0][1], cats[0], ",".join(cats[1:])))
    return routes, conflicts


def cmd_build(_a):
    routes, conflicts = from_bin()
    n, ncat = save(routes)
    print("ROUTES.txt: %d строк, %d категорий (%.1f МБ)"
          % (n, ncat, os.path.getsize(ROUTES) / 1048576.0))
    if conflicts:
        out = os.path.join(CROWD, "sync", "reports", "route_conflicts.csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["english", "оставлено", "снято"])
            w.writerows(sorted(conflicts))
        print("строк лежало в двух категориях сразу: %d — разобрано, отчёт %s"
              % (len(conflicts), os.path.relpath(out, CROWD)))
        print("несогласный маршрут правится в %s" % os.path.basename(FIX))


def batch_hashes():
    """{hash: (english, файл)} по обычным батчам."""
    out = {}
    for fp in D.batch_files():
        if "/split/" in fp.replace(os.sep, "/"):
            continue
        try:
            rows = D.read_csv(fp)
        except Exception:
            continue
        for r in rows[1:]:
            if r and r[0].strip():
                out.setdefault(D.fnv1a_u16(r[0]), (r[0], os.path.basename(fp)))
    return out


def cmd_check(_a):
    routes, bat = load(), batch_hashes()
    known = set(D.DICT_NAMES)
    bad = collections.Counter(c for c in routes.values() if c not in known)
    no_route = [h for h in bat if h not in routes]
    no_batch = [h for h in routes if h not in bat]
    print("маршрутов %d | строк в обычных батчах %d" % (len(routes), len(bat)))
    print("строк без маршрута (уедут в «%s»): %d" % (DEFAULT, len(no_route)))
    for h in no_route[:6]:
        print("    %s" % bat[h][0][:64].replace("\n", " "))
    print("маршрутов без строки в батче (мусор в индексе): %d" % len(no_batch))
    if bad:
        print("маршруты в НЕИЗВЕСТНЫЕ категории: %s" % dict(bad))


def cmd_show(a):
    routes = load()
    for en in a:
        print("%-14s %s" % (routes.get(D.fnv1a_u16(en), "(нет: %s)" % DEFAULT),
                            en[:60]))


def cmd_set(a):
    """set <категория> <файл>: приписать строки из файла к категории."""
    cat, src = a[0], a[1]
    if cat not in D.DICT_NAMES:
        sys.exit("неизвестная категория: %s" % cat)
    routes = load()
    n = 0
    with open(src, encoding="utf-8-sig", newline="") as f:
        for r in csv.reader(f):
            if not r or not r[0].strip() or r[0].strip() == "english":
                continue
            h = D.fnv1a_u16(r[0])
            if routes.get(h) != cat:
                routes[h] = cat
                n += 1
    save(routes)
    print("переназначено в «%s»: %d" % (cat, n))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]
    if cmd == "build":
        cmd_build(rest)
    elif cmd == "check":
        cmd_check(rest)
    elif cmd == "show":
        cmd_show(rest)
    elif cmd == "set":
        cmd_set(rest)
    else:
        sys.exit(__doc__)
