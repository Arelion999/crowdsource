#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вернуть потерянный сегмент описания предмета, взяв его перевод из корпуса.

    python tools/segfix.py check      # что восстановится
    python tools/segfix.py batches    # починить в батчах

В описаниях предметов «потерянный перенос строки» почти всегда означает не
сдвинувшийся разрыв, а ПРОПАВШИЙ КУСОК: в оригинале «Double-click to open.\\n
Contains materials…», а в переводе осталась только вторая фраза. Расставлять там
переносы бессмысленно — разделять нечего.

Зато первый сегмент у предметов почти всегда шаблонный, и в корпусе он переведён
сотни раз отдельной записью. Поэтому берём перевод оттуда и возвращаем сегмент
на его место в структуре оригинала.

Только описания предметов: в письмах и прозе пропадают обращения и подписи,
которых в корпусе нет, и там нужен человек.
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

ITEM = re.compile(r"Double-click|Consumed on use|Requires|Salvage|"
                  r"<c=@flavor>|Created in the Mystic Forge", re.I)


def corpus():
    out = {}
    for _h, (en, ru, _c) in D.load_map(D.OUR_BIN).items():
        if en and ru:
            out.setdefault(en.strip().rstrip(","), ru.strip())
    return out


def rebuild(en, ru, tr):
    """Собрать перевод по структуре оригинала, вернув недостающие сегменты."""
    segs = [s.strip().rstrip(",") for s in en.split("\n")]
    if len(segs) < 2 or len(segs) > 4:
        return None
    have = ru.strip()
    # Восстанавливаем ТОЛЬКО то, чего в переводе нет вовсе. Если перевод
    # сегмента там уже есть — пусть даже другой, — трогать нельзя: подстановка
    # из корпуса не заменит его, а припишет второй копией. Так «Наполнено…
    # растущему спрингеру» получило сверху «Содержит… раптору».
    n_have = have.count("\n") + 1
    if n_have != 1:
        return None
    out, restored = [], 0
    for s in segs[:-1]:
        if not s:
            out.append("")
            continue
        known = tr.get(s)
        if not known:
            return None
        # похоже, этот сегмент в переводе уже есть — значит потерян не он
        if _overlap(known, have):
            return None
        out.append(known)
        restored += 1
    if not restored:
        return None
    return "\n".join(out + [have])


def _stems(s):
    """Основы значимых слов: падежи сравнивать целиком нельзя —
    «питательные» и «питательными» это одно слово."""
    return {w[:5] for w in re.findall(r"[А-Яа-яЁё]{5,}", s.lower())}


def _overlap(a, b):
    """Похоже ли, что этот сегмент в переводе уже есть — пусть в другой форме."""
    sa = list(_stems(a))[:6]
    sb = _stems(b)
    return sum(1 for w in sa if w in sb) >= 2


def survey():
    tr = corpus()
    good, why = {}, collections.Counter()
    for fp in D.batch_files():
        rows = list(csv.reader(io.StringIO(open(fp, "rb").read().decode("utf-8"))))
        if not rows or rows[0][:1] != ["english"]:
            continue
        for r in rows[1:]:
            en, ru = r[0], (r[1] if len(r) > 1 else "")
            if not en.strip() or not ru.strip() or "\n" not in en:
                continue
            if not ITEM.search(en):
                continue
            if en.count("\n") <= ru.count("\n"):
                continue
            new = rebuild(en, ru, tr)
            if not new or new == ru:
                continue
            if _validate is not None:
                was = len(_validate.check_row(en, ru)[0])
                if len(_validate.check_row(en, new)[0]) > was:
                    why["отклонено линтером"] += 1
                    continue
            good[en] = (ru, new)
            why["к восстановлению"] += 1
    return good, why


def cmd_check(_a):
    good, why = survey()
    for k, n in why.most_common():
        print("   %5d  %s" % (n, k))
    for en, (was, new) in list(good.items())[:8]:
        print("\n  EN    %s" % en[:100].replace("\n", "\\n"))
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
    print("восстановлено ячеек батчей: %d в %d файлах" % (n, files))


CMDS = {"check": cmd_check, "batches": cmd_batches}
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
