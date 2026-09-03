#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перенос строк из «основного» в профильные категории по спискам.

    python tools/relabel.py plan          # что переедет и куда
    python tools/relabel.py apply         # переложить (с бэкапом)

Зачем. Категория словаря — это группа отключения в игре. «Основной» вырос до
162 тысяч строк и не гасится ничем: игрок не может выключить диалоги, оставив
интерфейс. Списки для разбора приходят снаружи (`split/*.csv`, колонка
`english`), перевод из них НЕ берётся — только принадлежность к группе.

Списки версионируются в `split/`, потому что раскладка обязана быть
воспроизводимой: сама она живёт только в bin, а bin в git не лежит.
"""
import collections, csv, glob, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import dict_tool as D

# Ярлык списка -> категория словаря. Берём только те группы, что выдержали
# проверку выборкой: «предметы» и «прочее» из присланного набора сюда не входят —
# API не подтвердил в них ни одного предмета, а в выборке нашлись умения и
# счётчики событий.
TARGET = {
    "диалоги": "npc_dialogue",
    "задания": "events",
    "книги": "books",
    "почта": "mail",
    "интерфейс": "interface",
    "уведомления": "notifications",
}


def lists():
    """{хеш: категория} по спискам split/*.csv."""
    out = {}
    csv.field_size_limit(10 ** 7)
    for fp in sorted(glob.glob(os.path.join(CROWD, "split", "*.csv"))):
        label = os.path.basename(fp)[:-4]
        cat = TARGET.get(label)
        if not cat:
            continue
        for r in D.read_csv(fp)[1:]:
            if r and r[0].strip():
                out[D.fnv1a_u16(r[0])] = cat
    return out


def survey():
    want = lists()
    sections = D.read_sections(D.OUR_BIN)
    move = collections.Counter()
    skip = collections.Counter()
    for name, es in sections:
        cur = name.partition("\x1f")[0]
        for h, en, ru in es:
            tgt = want.get(h)
            if not tgt or tgt == cur:
                continue
            if cur == "основной":
                move[tgt] += 1
            else:
                skip[cur] += 1
    return sections, want, move, skip


def cmd_plan():
    _s, want, move, skip = survey()
    print("строк в списках: %d" % len(want))
    print("\nпереедет из «основного»:")
    for k, v in move.most_common():
        print("  %-16s %6d" % (k, v))
    print("  %-16s %6d" % ("ВСЕГО", sum(move.values())))
    if skip:
        print("\nне тронем — лежат не в «основном»:")
        for k, v in skip.most_common(8):
            print("  %-24s %6d" % (k, v))


def cmd_apply():
    sections, want, move, _skip = survey()
    if not move:
        print("нечего переносить")
        return
    # Собираем заново: строку вынимаем из «основного» и кладём в целевую секцию.
    by_name = collections.OrderedDict((n, list(es)) for n, es in sections)
    short = {n.partition("\x1f")[0]: n for n in by_name}
    for cat in set(move):
        if cat not in short:
            key = "%s\x1f%s" % (cat, D.DICT_NAMES.get(cat, cat))
            by_name[key] = []
            short[cat] = key
    main_key = short["основной"]
    keep, moved = [], 0
    for h, en, ru in by_name[main_key]:
        tgt = want.get(h)
        if tgt and tgt != "основной":
            by_name[short[tgt]].append((h, en, ru))
            moved += 1
        else:
            keep.append((h, en, ru))
    by_name[main_key] = keep
    bak = D.backup(D.OUR_BIN)
    ncat, total = D.write_bin(D.OUR_BIN, list(by_name.items()))
    print("перенесено: %d | в bin %d категорий, %d записей" % (moved, ncat, total))
    print("бэкап: %s" % os.path.relpath(bak, D.ROOT))


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else ""
    if a == "plan":
        cmd_plan()
    elif a == "apply":
        cmd_apply()
    else:
        sys.exit(__doc__)
