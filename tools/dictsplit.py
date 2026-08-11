#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Развести названия и описания по разным словарям.

    python tools/dictsplit.py plan     # что куда переедет, без записи
    python tools/dictsplit.py apply    # бэкап в .dict_bak и запись bin

Зачем. Категория словаря — это группа отключения в игре: игрок гасит «Предметы:
названия» и видит английские названия для вики и торговли, а описания при этом
остаются русскими. Пока названия и описания лежат в одной категории, такой
выключатель невозможен: `items` (47 708 записей) содержала и то и другое, а
`achievements` и `skills` — тем более.

Раскладку берём из списков `dict_<класс>_<names|descriptions>.csv` (каталог
`--from`, по умолчанию `sync/api/split`): нас в них интересует ТОЛЬКО первая
колонка — какие английские строки в игре являются названием, а какие описанием.
Чужой перевод из второй колонки не берётся: в bin остаётся наш текст, тексты
записей эта операция не трогает вовсе, меняется только категория.

Записи, которых в списках нет, раскладываются по источнику: то, что лежало в
«Предметы: названия», — название (там весь гардероб, `%str1%%str2%…%str3%%str4%`),
остальное — описание (обрывки флейвора, «Дважды щёлкните…», `<c=@abilitytype>`).
"""
import csv, os, sys, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dict_tool as D

DEFAULT_FROM = os.path.join(CROWD, "sync", "api", "split")

# класс -> (категория-источник по умолчанию для остатка, id и подпись двух новых)
SPLIT = {
    "item": [("item_names", "Предметы: названия"),
             ("item_descriptions", "Предметы: описания")],
    "achievement": [("achievement_names", "Достижения: названия"),
                    ("achievement_descriptions", "Достижения: описания")],
    "skill": [("skill_names", "Умения: названия"),
              ("skill_descriptions", "Умения: описания")],
}
# что разбираем: категория в bin -> (класс, куда девать остаток: 0 названия, 1 описания)
SOURCE = {
    "items": ("item", 1),
    "items_names": ("item", 0),
    "achievements": ("achievement", 1),
    "skills": ("skill", 1),
}


def load_lists(src):
    """hash -> (класс, 0|1). Списки читаем только ради первой колонки."""
    by = {}
    for cls in SPLIT:
        for side, fname in ((0, "names"), (1, "descriptions")):
            fp = os.path.join(src, "dict_%s_%s.csv" % (cls, fname))
            if not os.path.exists(fp):
                sys.exit("нет файла списка: %s" % fp)
            n = 0
            for r in list(csv.reader(open(fp, encoding="utf-8-sig")))[1:]:
                if r and r[0].strip():
                    by.setdefault(D.fnv1a_u16(r[0]), (cls, side))
                    n += 1
            print("  %-34s %6d" % (os.path.basename(fp), n))
    return by


def build(src):
    """Возвращает (новые секции, отчёт)."""
    lists = load_lists(src)
    sections = D.read_sections(D.OUR_BIN)
    moved = collections.defaultdict(collections.Counter)
    rest = collections.Counter()
    buckets = collections.defaultdict(list)
    keep = []
    for name, es in sections:
        cat = name.partition("\x1f")[0]
        if cat not in SOURCE:
            keep.append((name, es))
            continue
        cls, default_side = SOURCE[cat]
        for h, en, ru in es:
            hit = lists.get(h)
            # чужой класс не указ: строка из «achievements» остаётся достижением,
            # даже если такое же название есть в списке предметов
            side = hit[1] if hit and hit[0] == cls else default_side
            tgt = SPLIT[cls][side][0]
            buckets[tgt].append((h, en, ru))
            moved[cat][tgt] += 1
            if not hit or hit[0] != cls:
                rest[cat] += 1

    # Столкновения: один хеш приезжает в одну категорию из двух источников
    # (`items` и `items_names` держали копии одной записи). Победителя выбираем
    # не по порядку, а по тексту: латиница в переводе — это непокрытое слоем имя
    # («Реликвия Dwayna» против «Реликвия Двайны»), такая копия хуже.
    def worse(ru):
        return sum(1 for c in ru if "A" <= c <= "Z" or "a" <= c <= "z")

    clash = []
    for tgt, es in buckets.items():
        seen, order = {}, []
        for h, en, ru in es:
            if h not in seen:
                seen[h] = (h, en, ru)
                order.append(h)
                continue
            old = seen[h][2]
            if old == ru:
                continue
            if worse(ru) < worse(old):
                seen[h] = (h, en, ru)
                clash.append((tgt, en, ru, old))
            else:
                clash.append((tgt, en, old, ru))
        buckets[tgt] = [seen[h] for h in order]

    new = []
    for name, es in keep:
        new.append((name, es))
    for cls, sides in SPLIT.items():
        for tgt, disp in sides:
            if buckets.get(tgt):
                new.append(("%s\x1f%s" % (tgt, disp), buckets[tgt]))
    # порядок как у csv_to_bin: dict_* по алфавиту, затем pn_*, затем основной/выученные
    def key(item):
        cat = item[0].partition("\x1f")[0]
        if cat == "основной":
            return (3, "")
        if cat == "выученные":
            return (4, "")
        if cat.startswith("pn_"):
            return (2, cat)
        return (1, cat)
    new.sort(key=key)
    return new, moved, rest, clash


def report(moved, rest, clash, new):
    print("\nчто куда переезжает:")
    for cat in sorted(moved):
        tot = sum(moved[cat].values())
        parts = ", ".join("%s %d" % (k, v) for k, v in moved[cat].most_common())
        print("  %-14s %6d -> %s   (не было в списках: %d)"
              % (cat, tot, parts, rest[cat]))
    if clash:
        print("\nодин хеш из двух источников с РАЗНЫМ переводом: %d" % len(clash))
        for tgt, en, a, b in clash[:10]:
            print("  %-18s %-40s\n     остаётся %s\n     отброшен %s"
                  % (tgt, (en or "")[:40], a[:60], b[:60]))
    print("\nсловари после правки:")
    for name, es in new:
        cat, _, disp = name.partition("\x1f")
        if cat in [c for s in SPLIT.values() for c, _d in s]:
            print("  %-26s %-28s %7d" % (cat, disp, len(es)))
    print("  ... всего категорий %d, записей %d"
          % (len(new), sum(len(es) for _n, es in new)))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    src = DEFAULT_FROM
    if "--from" in sys.argv:
        src = sys.argv[sys.argv.index("--from") + 1]
    if cmd not in ("plan", "apply"):
        sys.exit(__doc__)
    print("списки из %s:" % os.path.relpath(src, CROWD))
    new, moved, rest, clash = build(src)
    report(moved, rest, clash, new)
    # Гейт: операция перекладывает записи, а не правит текст. Значит соответствие
    # «хеш -> перевод» обязано сохраниться для всех, кроме схлопнутых дублей.
    old_sec = D.read_sections(D.OUR_BIN)
    before = sum(len(es) for _n, es in old_sec)
    after = sum(len(es) for _n, es in new)
    was, now = {}, {}
    for _n, es in old_sec:
        for h, _en, ru in es:
            was.setdefault(h, set()).add(ru)
    for _n, es in new:
        for h, _en, ru in es:
            now.setdefault(h, set()).add(ru)
    lost_keys = [h for h in was if h not in now]
    lost_text = [h for h in was if h in now and not (was[h] & now[h])]
    print("\nключей было %d, стало %d | потеряно ключей: %d | сменился текст: %d"
          % (len(was), len(now), len(lost_keys), len(lost_text)))
    if lost_keys or lost_text:
        sys.exit("! запись отменена: перекладка не должна терять записи")
    if after != before:
        print("записей было %d, стало %d (разница %+d — схлопнулись дубли)"
              % (before, after, after - before))
    if cmd == "plan":
        print("\nplan: запись не выполнена")
        return
    bak = D.backup(D.OUR_BIN)
    D.write_bin(D.OUR_BIN, new)
    print("\nбэкап: %s" % os.path.relpath(bak, CROWD))
    print("записано: %s" % os.path.relpath(D.OUR_BIN, CROWD))
    chk = D.read_sections(D.OUR_BIN)
    print("перечитано: категорий %d, записей %d"
          % (len(chk), sum(len(es) for _n, es in chk)))


if __name__ == "__main__":
    main()
