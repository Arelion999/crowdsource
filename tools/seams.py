#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
seams.py — ручная доводка швов, которые не взяла машина.

После `dict_tool.py newlines` остаются строки, где перенос растворился в прозе
без единой зацепки: ни склейки, ни числа, ни маркера. Такое место видно только
по смыслу английского оригинала, поэтому шов ставится руками.

    python seams.py export work.csv        выгрузить оставшиеся (hash, english, наш перевод)
    python seams.py apply work.csv         влить колонку «fixed» обратно в bin
    python seams.py apply work.csv --dry   только показать, что будет сделано

Формат рабочего файла: hash, english, ru, fixed. Заполняется колонка «fixed» —
тот же перевод с расставленными переносами; пустая строка означает «пропустить».

ЗАЩИТА. При вливании проверяется, что правка ТОЛЬКО расставила переносы: текст
без пробельных символов обязан совпасть с исходным до символа. Переписать перевод
через этот путь нельзя — для этого есть батчи и merge. Ещё проверяется, что число
разрывов совпало с оригиналом: шов не может появиться там, где английский не рвётся.
"""
import argparse, csv, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dict_tool as T                                            # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NOSPACE = re.compile(r"\s+")


def bare(s):
    """Текст без пробельных символов — по нему сверяем, что правка ничего не переписала."""
    return NOSPACE.sub("", s)


def remaining():
    """[(hash, english, наш перевод)] — где переносов в переводе меньше, чем в оригинале."""
    out = []
    for h, (en, ru, _c) in T.load_map(T.OUR_BIN).items():
        if en and ru and en.count("\n") > ru.count("\n"):
            out.append((h, en, ru))
    out.sort(key=lambda x: (x[1].count("\n"), len(x[1])))
    return out


def cmd_export(a):
    rows = remaining()
    with open(a.file, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["hash", "english", "ru", "fixed"])
        for h, en, ru in rows:
            w.writerow(["%016x" % h, en, ru, ""])
    print("выгружено: %d -> %s" % (len(rows), a.file))


def cmd_apply(a):
    ours = T.load_map(T.OUR_BIN)
    changes, bad, empty = {}, [], 0
    with open(a.file, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            fixed = (r.get("fixed") or "")
            if not fixed.strip():
                empty += 1
                continue
            h = int(r["hash"], 16)
            if h not in ours:
                bad.append((r["hash"], "записи нет в bin"))
                continue
            en, ru = ours[h][0], ours[h][1]
            if bare(fixed) != bare(ru):
                bad.append((r["hash"], "правка меняет текст, а не только переносы"))
                continue
            if fixed.count("\n") != en.count("\n"):
                bad.append((r["hash"], "переносов %d, в оригинале %d"
                            % (fixed.count("\n"), en.count("\n"))))
                continue
            if fixed == ru:
                empty += 1
                continue
            changes[h] = fixed
    print("принято: %d | пропущено: %d | отклонено: %d" % (len(changes), empty, len(bad)))
    for hx, why in bad[:20]:
        print("  отклонено %s — %s" % (hx, why))
    if a.dry:
        for h in list(changes)[:5]:
            print("\n  EN    %r\n  было  %r\n  стало %r"
                  % (ours[h][0][:90], ours[h][1][:90], changes[h][:90]))
        print("\n(сухой прогон; без --dry правки будут записаны)")
        return
    T.apply_changes(changes, {}, "ручная расстановка швов")


def cmd_anchors(a):
    """Расставить швы по якорям: «hash<TAB>кусок<TAB>кусок…».

    Якорь — начало новой строки в НАШЕМ переводе (несколько первых слов). Так
    правку можно задать, не переписывая текст: инструмент сам найдёт место и
    вставит ту пачку переносов, что стоит в оригинале. Якоря ищутся по порядку,
    каждый следующий — правее предыдущего.
    """
    ours = T.load_map(T.OUR_BIN)
    out, bad = {}, []
    for ln in open(a.file, encoding="utf-8"):
        ln = ln.rstrip("\n")
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        parts = ln.split("\t")
        h = int(parts[0], 16)
        if h not in ours:
            bad.append((parts[0], "записи нет в bin"))
            continue
        en, ru = ours[h][0], ours[h][1]
        runs = [r for _p, r in T.nl_flat(en)[1]]
        anchors = [p for p in parts[1:] if p.strip()]
        if len(anchors) != len(runs):
            bad.append((parts[0], "якорей %d, разрывов в оригинале %d"
                        % (len(anchors), len(runs))))
            continue
        # cur — начало неразобранного куска, seek — откуда искать следующий якорь.
        # Разделять их обязательно: в блоках характеристик якоря повторяются
        # («+10%%» дважды), и поиск с cur нашёл бы тот же самый.
        new, cur, seek, ok = "", 0, 0, True
        for anchor, run in zip(anchors, runs):
            i = ru.find(anchor, seek)
            if i < 0:
                bad.append((parts[0], "якорь не найден: %r" % anchor[:40]))
                ok = False
                break
            new += ru[cur:i].rstrip() + run
            cur, seek = i, i + max(1, len(anchor))
        if not ok:
            continue
        new += ru[cur:]
        out[h] = new
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["hash", "english", "ru", "fixed"])
        for h, new in out.items():
            w.writerow(["%016x" % h, ours[h][0], ours[h][1], new])
    print("швов расставлено: %d | отклонено: %d -> %s" % (len(out), len(bad), a.out))
    for hx, why in bad:
        print("  %s — %s" % (hx, why))


def main():
    ap = argparse.ArgumentParser(description="Ручная доводка швов")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export", help="выгрузить оставшиеся строки")
    p.add_argument("file")
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("anchors", help="расставить швы по якорям начала строки")
    p.add_argument("file")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_anchors)
    p = sub.add_parser("apply", help="влить колонку fixed обратно в bin")
    p.add_argument("file")
    p.add_argument("--dry", action="store_true")
    p.set_defaults(fn=cmd_apply)
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
