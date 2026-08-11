#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Канон боевых терминов — только в механическом тексте.

    python tools/combat.py check     # что и где заменится
    python tools/combat.py apply     # применить к bin (бэкап + гейт линтера)

Почему отдельный инструмент, а не правило в `dict_tool.canon`: канон боевых
терминов действует НЕ везде. «The foolish vigor of youth» — обычное слово, там
«энергия юности» вернее «энергичности», а «Chilled to the Bone!» — название
умения с идиомой. Канон обязателен только в описаниях умений и талантов, а
границу знает граф (`index.py build` -> таблица `ctx`, вид «механика»).

Заменяем существительное на существительное. Глагольные формы не трогаем: у
`Launch` в описаниях стоит «запустите» про снаряд, у `Immobilize` —
«обездвижьте», у `Burning` — «поджигая», и всё это законно.

Эталон канона — https://ru.gw2skills.net/wiki (сверено 2026-08-11, совпадает с
GLOSSARY.md дословно).
"""
import os, re, sqlite3, sys, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
DB = os.path.join(CROWD, "sync", "index.db")
sys.path.insert(0, HERE)
sys.path.insert(0, CROWD)
import dict_tool as D
try:
    import validate as _validate
except Exception:
    _validate = None

# термин -> ((неверная форма, канон в том же падеже), ...)
# Порядок важен: длинные окончания первыми, иначе «могущества» съест «могущество».
FIX = {
    "Might": (("могуществом", "Мощью"), ("могущества", "Мощи"),
              ("могуществу", "Мощи"), ("могуществе", "Мощи"),
              ("могущество", "Мощь")),
    "Protection": (("защитой", "Протекцией"), ("защиты", "Протекции"),
                   ("защите", "Протекции"), ("защиту", "Протекцию"),
                   ("защита", "Протекция")),
    "Stability": (("стабильностью", "Устойчивостью"),
                  ("стабильности", "Устойчивости"),
                  ("стабильность", "Устойчивость")),
    "Resistance": (("сопротивлением", "Сопротивляемостью"),
                   ("сопротивления", "Сопротивляемости"),
                   ("сопротивлению", "Сопротивляемости"),
                   ("сопротивление", "Сопротивляемость")),
    "Torment": (("мучениями", "Болью"), ("мучением", "Болью"),
                ("мучений", "Боли"), ("мучения", "Боли"),
                ("мучению", "Боли"), ("мучение", "Боль")),
    "Quickness": (("быстротой", "Проворством"), ("быстроты", "Проворства"),
                  ("быстроте", "Проворстве"), ("быстроту", "Проворство"),
                  ("быстрота", "Проворство")),
    # «сбивание с ног» — это всё название целиком, и менять надо его целиком,
    # иначе выходит «нокдаун с ног». Между словом и «с ног» бывает вставка
    # («сбивания ЦЕЛИ с ног»), поэтому допускаем до двух слов и возвращаем их.
    "Knockdown": ((r"сбиванием((?:\s+\w+){0,2})\s+с\s+ног", r"Нокдауном\1"),
                  (r"сбивания((?:\s+\w+){0,2})\s+с\s+ног", r"Нокдауна\1"),
                  (r"сбиванию((?:\s+\w+){0,2})\s+с\s+ног", r"Нокдауну\1"),
                  (r"сбивание((?:\s+\w+){0,2})\s+с\s+ног", r"Нокдаун\1"),
                  ("сбиванием", "Нокдауном"), ("сбивания", "Нокдауна"),
                  ("сбиванию", "Нокдауну"), ("сбивание", "Нокдаун")),
    "Cripple": (("увечьем", "Хромотой"), ("увечья", "Хромоты"),
                ("увечью", "Хромоте")),
    "Chilled": (("охлаждением", "Заморозкой"), ("охлаждения", "Заморозки"),
                ("охлаждению", "Заморозке")),
    "Blindness": (("ослеплением", "Слепотой"), ("ослепления", "Слепоты"),
                  ("ослеплению", "Слепоте")),
    "Vigor": (("бодростью", "Энергичностью"), ("бодрости", "Энергичности"),
              ("бодрость", "Энергичность")),
    "Regeneration": (("восстановлением", "Регенерацией"),
                     ("восстановления", "Регенерации")),
}
# Пары, где у исходного слова именительный и винительный совпадают, а у канона
# нет: «увечье» среднего рода, «хромота» женского. Падеж выбираем по глаголу
# перед словом, иначе выходит «наносящих хромота».
AMBIG = {
    "Swiftness": ("стремительность", "Быстрота", "Быстроту"),
    "Cripple": ("увечье", "Хромота", "Хромоту"),
    "Chilled": ("охлаждение", "Заморозка", "Заморозку"),
    "Blindness": ("ослепление", "Слепота", "Слепоту"),
    "Regeneration": ("восстановление", "Регенерация", "Регенерацию"),
}
ACC_VERB = re.compile(r"(?<![А-Яа-яЁё])(?:получ\w+|дару\w+|даров\w+|подар\w+|"
                      r"наклад\w+|налож\w+|нанос\w+|нанес\w+|вызыва\w+|вызов\w+|"
                      r"вызыв\w+|причин\w+|прим\w+|обрет\w+|"
                      r"добав\w+|включ\w+|даёт|дает|дайте|снима\w+|снять|снимите|"
                      r"убира\w+|теря\w+)(?![А-Яа-яЁё])", re.I)


def is_acc(before):
    """Винительный ли падеж — по управляющему глаголу в ТОМ ЖЕ предложении.

    Окном в пару слов не обойтись: в описаниях умений сплошные перечисления
    («Дарует ярость, мощь и быстроту»), и глагол оказывается далеко. Поэтому
    режем по границе предложения и ищем глагол во всём остатке.
    """
    tail = re.split(r"[.;!?]|<br>|\n", before)[-1]
    return bool(ACC_VERB.search(tail))


def load():
    if not os.path.exists(DB):
        sys.exit("нет sync/index.db — сначала `tools/index.py build`")
    db = sqlite3.connect(DB)
    mech = {r[0] for r in db.execute("SELECT hash FROM ctx WHERE kind='механика'")}
    return db, mech


def fix_one(en, ru):
    """Вернуть исправленный перевод и список сделанных замен."""
    out, done = ru, []
    for term, pairs in FIX.items():
        if not re.search(r"(?<![A-Za-z])" + term + r"(?![A-Za-z])", en, re.I):
            continue
        for wrong, right in pairs:
            # Регистр берём символьным классом, а не группой: в шаблонах есть
            # свои скобки («сбивание ЦЕЛИ с ног»), и группа регистра сбила бы
            # им нумерацию — \1 в замене указывал бы на букву.
            rx = re.compile(r"(?<![А-Яа-яЁё])[" + wrong[0].upper() + wrong[0]
                            + "]" + wrong[1:] + r"(?![А-Яа-яЁё])")
            def rep(m, right=right):
                t = right if m.group(0)[0].isupper() else right[0].lower() + right[1:]
                return m.expand(t)
            new = rx.sub(rep, out)
            if new != out:
                done.append("%s: %s -> %s" % (term, wrong, right))
                out = new
    for term, (wrong, nom, acc) in AMBIG.items():
        if not re.search(r"(?<![A-Za-z])" + term + r"(?![A-Za-z])", en, re.I):
            continue
        rx = re.compile(r"(?<![А-Яа-яЁё])(" + wrong[0].upper() + "|" + wrong[0]
                        + ")" + wrong[1:] + r"(?![А-Яа-яЁё])")
        cur = out
        def rep(m, nom=nom, acc=acc, cur=cur):
            form = acc if is_acc(cur[:m.start()]) else nom
            return form if m.group(1).isupper() else form[0].lower() + form[1:]
        new = rx.sub(rep, out)
        if new != out:
            done.append("%s: %s -> %s/%s" % (term, wrong, nom, acc))
            out = new
    return out, done


def is_skill_name(en):
    """Название умения, а не описание: несколько слов и без знака конца фразы.

    Решение пользователя 2026-08-11: в НАЗВАНИЯХ умений оставляем литературную
    форму — «Раскол мучений» читается лучше, чем «Раскол боли». Канон обязателен
    в описаниях. Голое имя самого термина («Torment», «Swiftness») — не название
    умения, а сам термин, и его канон касается.
    """
    en = en.strip()
    return (len(en.split()) > 1 and not re.search(r"[.!?:]", en)
            and len(en) < 48)


def survey():
    db, mech = load()
    changes, ex, refused = {}, [], 0
    skipped_names = 0
    for hh, en, ru in db.execute("SELECT hash, english, ru FROM string"):
        if hh not in mech or not en or not ru:
            continue
        if is_skill_name(en):
            skipped_names += 1
            continue
        new, done = fix_one(en, ru)
        if new == ru:
            continue
        if _validate is not None:
            was = len(_validate.check_row(en, ru)[0])
            if len(_validate.check_row(en, new)[0]) > was:
                refused += 1
                continue
        changes[hh] = (en, ru, new, done)
        if len(ex) < 12:
            ex.append((en, ru, new, done))
    return changes, ex, refused


def cmd_check(_a):
    changes, ex, refused = survey()
    print("строк механики под правку: %d | отклонено гейтом: %d"
          % (len(changes), refused))
    per = collections.Counter(d.split(":")[0] for _e, _r, _n, dn in changes.values()
                              for d in dn)
    for t, n in per.most_common():
        print("   %5d  %s" % (n, t))
    print()
    for en, ru, new, done in ex:
        print("  EN    %s" % en[:100])
        print("  было  %s" % ru[:100])
        print("  стало %s   [%s]" % (new[:100], "; ".join(done)))


def cmd_apply(_a):
    changes, _ex, refused = survey()
    if not changes:
        print("нечего менять")
        return
    by_en = {en: new for en, _ru, new, _d in changes.values()}
    upd = {}
    for h, (en, ru, _c) in D.load_map(D.OUR_BIN).items():
        if en in by_en and by_en[en] != ru:
            upd[h] = by_en[en]
    D.apply_changes(upd, {}, "канон боевых терминов")
    print("отклонено гейтом: %d" % refused)


def cmd_batches(_a):
    """Тот же канон по батчам — иначе bin и батчи разъедутся.

    Через `canonbatches` не выйдет: там правила `dict_tool.normalize`, а канон
    боевых терминов зависит от контекста строки, который знает только граф.
    """
    import csv, io
    good, _ex, _ref = survey()
    if not good:
        print("нечего менять")
        return
    by_en = {en: new for en, _ru, new, _d in good.values()}
    total = 0
    for fp in D.batch_files():
        rows = list(csv.reader(io.StringIO(open(fp, "rb").read().decode("utf-8"))))
        if not rows or rows[0][:1] != ["english"]:
            continue
        n = 0
        for r in rows[1:]:
            if len(r) < 2 or not r[0].strip() or not r[1].strip():
                continue
            want = by_en.get(r[0])
            if want and want != r[1]:
                if _validate is not None:
                    was = len(_validate.check_row(r[0], r[1])[0])
                    if len(_validate.check_row(r[0], want)[0]) > was:
                        continue
                r[1] = want
                n += 1
        if n:
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            open(fp, "wb").write(buf.getvalue().encode("utf-8"))
            total += n
    print("поправлено ячеек батчей: %d" % total)


CMDS = {"check": cmd_check, "apply": cmd_apply, "batches": cmd_batches}
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
