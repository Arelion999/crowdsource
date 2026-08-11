#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Взять описания умений с ru.gw2skills.net, сохранив нашу разметку.

    python tools/gw2s_import.py check    # что заменится, что отсеяно и почему
    python tools/gw2s_import.py apply    # применить (бэкап + гейт линтера)

ИСТОЧНИК: переводы принадлежат Gw2Skills.Net, https://ru.gw2skills.net —
сайт требует ссылку при использовании. Она стоит в README и в GLOSSARY.md.

Главная сложность. У них текст плоский, а игровая строка несёт разметку:
`<c=@abilitytype>Chain.</c> Bleed your foe…`, переносы, блоки
`<c=@reminder>…</c>`, плейсхолдеры `%num1%`. Вставить их текст целиком — значит
всё это потерять, а без разметки строка в игре ломается.

Поэтому заменяем ТОЛЬКО прозу и только там, где она в строке одна: разметка,
метка в теге и плейсхолдеры остаются нашими, на место единственного прозаического
куска встаёт их перевод. Строки с двумя и более кусками прозы (описание плюс
`<c=@reminder>`) не трогаем — их текст пришлось бы делить на части наугад.

Сверху стоит гейт линтера: правка принимается, только если набор тегов и
плейсхолдеров не изменился.
"""
import collections, csv, os, re, sqlite3, sys

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

SEP = re.compile(r"(<[^>]*>|\n|%\w+%|\[[^\]]*\])")


def skel(s):
    s = re.sub(r"%\w+%|<[^>]*>|\[[^\]]*\]", "", s)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def prose_parts(s):
    """Куски текста между разметкой, с их местом в списке токенов."""
    toks = SEP.split(s)
    idx = [i for i, t in enumerate(toks)
           if not SEP.fullmatch(t or "") and len((t or "").strip()) > 10]
    return toks, idx


def build(our_ru, their):
    """Наша разметка + их проза. None, если прозы не ровно один кусок."""
    toks, idx = prose_parts(our_ru)
    if len(idx) != 1:
        return None
    i = idx[0]
    lead = re.match(r"^\s*", toks[i]).group(0)
    tail = re.search(r"\s*$", toks[i]).group(0)
    toks[i] = lead + their.strip() + tail
    return "".join(toks)


def survey():
    theirs = {}
    fp = os.path.join(CROWD, "sync", "api", "gw2skills.csv")
    if not os.path.exists(fp):
        sys.exit("нет sync/api/gw2skills.csv — сначала `gw2skills.py fetch`")
    for r in list(csv.reader(open(fp, encoding="utf-8-sig")))[1:]:
        if len(r) >= 2:
            theirs.setdefault(r[0], r[1])

    db = sqlite3.connect(os.path.join(CROWD, "sync", "index.db"))
    want = [(n, d) for n, d in db.execute(
        "SELECT name, descr FROM skill WHERE descr<>''") if n in theirs]

    by = collections.defaultdict(list)
    for _h, (en, ru, _c) in D.load_map(D.OUR_BIN).items():
        if en and len(en) > 15:
            by[skel(en)].append((en, ru))

    good, why = {}, collections.Counter()
    for n, d in want:
        v = by.get(skel(d), [])
        if len(v) != 1:
            why["нет однозначной строки в словаре"] += 1
            continue
        en, ru = v[0]
        if not ru.strip():
            why["у нас пусто, разметку брать неоткуда"] += 1
            continue
        new = build(ru, theirs[n])
        if new is None:
            why["прозы больше одного куска"] += 1
            continue
        if new == ru:
            why["совпадает с нашим"] += 1
            continue
        if _validate is not None:
            # Числа и служебные символы сверяем СТРОГО с английским, а не
            # «не хуже прежнего»: их текст писался под свою вёрстку, и в нём
            # легко теряется число или процент, которых у нас не было и в
            # старом переводе — тогда правка прошла бы незамеченной.
            if _validate.numbers(en) - _validate.numbers_ru(new):
                why["число оригинала пропало"] += 1
                continue
            if _validate.tokens(en) != _validate.tokens(new):
                why["теги или плейсхолдеры разошлись"] += 1
                continue
            if en.count("%%") != new.count("%%"):
                why["разошлись знаки процента"] += 1
                continue
            # Кавычки и конечный знак — тоже символы оригинала, и их текст их
            # теряет: у нас «Создайте…», у них без кавычек вовсе.
            if len(re.findall(r'["“”«»]', en)) > len(re.findall(r'["“”«»]', new)):
                why["кавычки оригинала потеряны"] += 1
                continue
            if en.rstrip()[-1:] in ".!?" and new.rstrip()[-1:] not in ".!?…»\"":
                why["потерян конечный знак"] += 1
                continue
            if len(_validate.check_row(en, new)[0]) > len(_validate.check_row(en, ru)[0]):
                why["отклонено линтером"] += 1
                continue
        good[en] = (ru, new, n)
    return good, why


def cmd_check(_a):
    good, why = survey()
    print("к замене: %d" % len(good))
    for k, v in why.most_common():
        print("   %5d  %s" % (v, k))
    print()
    for en, (ru, new, n) in list(good.items())[:6]:
        print("### %s" % n)
        print("  EN    %s" % en[:110])
        print("  было  %s" % ru[:110])
        print("  стало %s\n" % new[:110])


def cmd_apply(_a):
    good, _why = survey()
    if not good:
        print("нечего менять")
        return
    upd = {}
    for h, (en, ru, _c) in D.load_map(D.OUR_BIN).items():
        if en in good and good[en][1] != ru:
            upd[h] = good[en][1]
    D.apply_changes(upd, {}, "описания умений (gw2skills.net)")


def cmd_batches(_a):
    """Довести те же строки в батчах: иначе bin и батчи разъедутся молча.

    Штатные команды сюда не достают: `fillbatches` закрывает пустые ячейки,
    `--repair` — только те, на что ругается линтер, а тут перевод менялся на
    лучший при чистом линте с обеих сторон.
    """
    good, _why = survey()
    n = 0
    for fp in D.batch_files():
        raw = open(fp, "rb").read().decode("utf-8")
        rows = list(csv.reader(__import__("io").StringIO(raw)))
        if not rows or rows[0][:1] != ["english"]:
            continue
        hit = 0
        for r in rows[1:]:
            if len(r) < 2 or not r[0].strip() or r[0] not in good:
                continue
            new = good[r[0]][1]
            if r[1].strip() != new.strip():
                r[1] = new
                hit += 1
        if hit:
            buf = __import__("io").StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            open(fp, "wb").write(buf.getvalue().encode("utf-8"))
            n += hit
    print("ячеек батчей подтянуто: %d" % n)


CMDS = {"check": cmd_check, "apply": cmd_apply, "batches": cmd_batches}
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
