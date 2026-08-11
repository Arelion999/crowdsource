#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вернуть перенос строки там, где шов ВИДЕН в тексте.

    python tools/gluefix.py check     # что найдено
    python tools/gluefix.py batches   # починить в батчах (источник истины)

Признак: знак конца фразы, за которым СРАЗУ идёт заглавная буква без пробела —
«…с зелеными прожилками.Можно обменять…», «Герой,Если вы не были героем…».
Так выглядит потерянный перенос: слова слиплись ровно на его месте.

Чего этот инструмент НЕ делает: не расставляет абзацы в длинной прозе. Там шва
в тексте не видно (перенос стоял между фразами, разделёнными пробелом), и место
приходится искать выравниванием — это `tools/seams.py` и ручная работа.

Правим батчи: bin собирается из них (README, «Порядок работы»).
"""
import csv, io, os, re, sys, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, CROWD)
import dict_tool as D
try:
    import validate as _validate
except Exception:
    _validate = None

# шов: точка/запятая/скобка, сразу за ней заглавная — без пробела
GLUE = re.compile(r"(?<=[.,!?:»)])(?=[А-ЯЁA-Z])")


def fix(en, ru):
    """Вставить переносы в видимые склейки, но не больше, чем не хватает."""
    need = en.count("\n") - ru.count("\n")
    if need <= 0:
        return None
    pos = [m.start() for m in GLUE.finditer(ru)]
    if not pos:
        return None
    # если склеек больше, чем недостающих переносов, выбрать какие — гадание
    if len(pos) > need:
        return None
    out, prev = [], 0
    for p in pos:
        out.append(ru[prev:p])
        prev = p
    out.append(ru[prev:])
    return "\n".join(out)


def survey():
    good, why = {}, collections.Counter()
    for fp in D.batch_files():
        rows = list(csv.reader(io.StringIO(open(fp, "rb").read().decode("utf-8"))))
        if not rows or rows[0][:1] != ["english"]:
            continue
        for r in rows[1:]:
            if len(r) < 2 or not r[0].strip() or not r[1].strip():
                continue
            new = fix(r[0], r[1])
            if not new or new == r[1]:
                continue
            if _validate is not None:
                was = len(_validate.check_row(r[0], r[1])[0])
                if len(_validate.check_row(r[0], new)[0]) > was:
                    why["отклонено линтером"] += 1
                    continue
            good[r[0]] = (r[1], new)
            why["к правке"] += 1
    return good, why


def cmd_check(_a):
    good, why = survey()
    for k, n in why.most_common():
        print("   %5d  %s" % (n, k))
    for en, (was, new) in list(good.items())[:8]:
        print("\n  EN    %s" % en[:100])
        print("  было  %s" % was[:100].replace("\n", "\\n"))
        print("  стало %s" % new[:100].replace("\n", "\\n"))


def cmd_batches(_a):
    good, _why = survey()
    if not good:
        print("нечего менять")
        return
    n = files = 0
    for fp in D.batch_files():
        rows = list(csv.reader(io.StringIO(open(fp, "rb").read().decode("utf-8"))))
        if not rows or rows[0][:1] != ["english"]:
            continue
        hit = 0
        for r in rows[1:]:
            if len(r) < 2 or r[0] not in good:
                continue
            was, new = good[r[0]]
            if r[1] == was:
                r[1] = new
                hit += 1
        if hit:
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            open(fp, "wb").write(buf.getvalue().encode("utf-8"))
            n += hit
            files += 1
    print("поправлено ячеек батчей: %d в %d файлах" % (n, files))


CMDS = {"check": cmd_check, "batches": cmd_batches}
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
