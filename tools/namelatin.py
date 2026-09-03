#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вернуть латиницу там, где имя переведено в тексте ДОСЛОВНО формой из слоя.

    python tools/namelatin.py plan     # что изменится
    python tools/namelatin.py apply

Зачем. Выключатель имён работает так: `pn_*` гасятся, и игрок видит оригинал —
но только если в самом тексте имя оставлено латиницей. Где общий словарь перевёл
имя внутри фразы, выключателем его уже не вернуть.

Правим только БЕЗУСЛОВНЫЙ случай: в переводе стоит ровно та русская форма,
которую держит слой, слово в слово и по границам слова. Тогда замена на латиницу
ничего не меняет для игрока — слой подставит обратно тот же самый текст, — а
выключатель начинает работать. Косвенные падежи («в Львиной Арке») не трогаем:
слой не склоняет, и подстановка дала бы «в Львиная Арка». Это работа для очереди
«имена в косвенных падежах», её делают руками.

Улика берётся у линтера (`validate.layer_name_lost`): имя есть в оригинале и
пропало из перевода. Одного этого мало — дополнительно требуем, чтобы русская
форма из слоя стояла в переводе целиком.
"""
import csv, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, CROWD)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import dict_tool as D
import validate as V

CYR = re.compile("[А-Яа-яЁё]")
RU_WORD = re.compile(r"[А-Яа-яЁё]+")
PN = "pn_"


def layer_ru(lower_words):
    """{английское имя: русская форма} из слоя pn_*, только пригодные к замене.

    Отсеиваем два сорта записей:

    * ОДНОСЛОВНЫЕ. В слое полно обычных слов («Guard», «Citizen», «Boots»), и
      кириллица на их месте в чужой фразе — нормальный перевод, а не имя.
    * НЕ ИМЕНА. В слое осели куски фраз («Assist Lyhr» -> «Помогите Lyhr»).
      Подставлять по ним латиницу значит вписывать в русский текст английский
      глагол. Признак: первое русское слово формы корпус обычно пишет СТРОЧНЫМ
      (модель падежей `validate.build_case_model`) — у имени так не бывает.
    """
    out, dropped = {}, 0
    for name, es in D.read_sections(D.OUR_BIN):
        if not name.partition("\x1f")[0].startswith(PN):
            continue
        for _h, en, ru in es:
            en, ru = en.strip(), ru.strip()
            if len(en.split()) < 2 or not ru or not CYR.search(ru) or ru == en:
                continue
            w = RU_WORD.search(ru)
            if w and w.group(0).lower() in lower_words:
                dropped += 1
                continue
            out[en] = ru
    print("имён слоя пригодно к замене: %d (отсеяно как не-имя: %d)"
          % (len(out), dropped))
    return out


def whole(rf):
    """Форма целиком, по границам слова.

    «Бава Нисос» внутри «Бава Нисосе» — косвенный падеж; подстановка без границы
    оставила бы «Bava Nisosе» с приклеенным русским окончанием.

    Регистр не важен: корпус пишет «Шторм осколков», слой — «Шторм Осколков».
    Это одна и та же форма, и замена так же обратима — разница только в том, чья
    заглавная победит при подстановке, а решает это слой.
    """
    return re.compile(r"(?<![А-Яа-яЁёA-Za-z])%s(?![А-Яа-яЁёA-Za-z])"
                      % re.escape(rf), re.IGNORECASE)


def plan():
    files = [f for f in D.batch_files()]
    lay = layer_ru(V.build_case_model(files))
    rx = {nm: whole(rf) for nm, rf in lay.items()}
    hits, per_name = [], {}
    for fp in files:
        rows = D.read_csv(fp)
        if not rows or rows[0][:1] != ["english"]:
            continue
        for i, r in enumerate(rows[1:], start=2):
            if len(r) < 2 or not r[0].strip() or not r[1].strip():
                continue
            en, ru = r[0], r[1]
            new, used = ru, []
            for nm in V.layer_name_lost(en, ru):
                if nm not in lay or not rx[nm].search(new):
                    continue
                new = rx[nm].sub(lambda _m, s=nm: s, new)
                used.append(nm)
            if used:
                hits.append((fp, i, en, ru, new))
                for nm in used:
                    per_name[nm] = per_name.get(nm, 0) + 1
    return hits, per_name


def main(apply):
    hits, per_name = plan()
    print("строк, где имя стоит дословной формой слоя: %d (имён %d)"
          % (len(hits), len(per_name)))
    for nm, n in sorted(per_name.items(), key=lambda kv: -kv[1])[:12]:
        print("   %-40s %5d" % (nm[:40], n))
    for fp, i, _en, ru, new in hits[:6]:
        print("  %s:%d" % (os.path.relpath(fp, CROWD), i))
        print("     было  %s" % ru[:92].replace("\n", " "))
        print("     стало %s" % new[:92].replace("\n", " "))
    if not apply:
        print("(план; для записи apply)")
        return
    by_file = {}
    for fp, i, _en, _ru, new in hits:
        by_file.setdefault(fp, {})[i] = new
    for fp, fix in by_file.items():
        rows = D.read_csv(fp)
        for i, new in fix.items():
            rows[i - 1][1] = new
        D.write_csv(fp, rows)
    print("переписано файлов: %d, строк: %d" % (len(by_file), len(hits)))


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else ""
    if a in ("plan", "apply"):
        main(a == "apply")
    else:
        sys.exit(__doc__)
