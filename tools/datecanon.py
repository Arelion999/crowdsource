#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приводит даты в батчах к канону GLOSSARY.md.

Два независимых прохода:

1. Семья «This appears to be a journal scrap dated …» — перевод собирается
   заново из английского по шаблону, потому что в корпусе она переведена
   десятком разных способов («13-м днём месяца Зефир», «65 годом эпохи
   Скиона», «датированный Сционом»), и латать их поштучно бессмысленно.
2. Свободный текст — трогается только обозначение эры: «AE» и самодельные
   «от Эпохи Дракона», «г.В.», «г. Э.Д.» становятся «ПИ». Формулировки и
   переносы строк остаются как были.

Запуск без --apply печатает план.
"""
import csv, io, os, re, sys, glob

os.chdir(r"C:/Games/Guild Wars 2/glyphCore/crowdsource")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GEN = {"Zephyr": "Зефира", "Phoenix": "Феникса", "Scion": "Отпрыска", "Colossus": "Колосса"}
# без числа день не назван, поэтому месяц идёт в творительном: «датированный Отпрыском»
INS = {"Zephyr": "Зефиром", "Phoenix": "Фениксом", "Scion": "Отпрыском", "Colossus": "Колоссом"}

SCRAP = re.compile(r"^This appears to be a journal scrap dated (?:(\d{1,3}) )?"
                   r"(Zephyr|Phoenix|Scion|Colossus), (\d{3,4}) AE\.$")
DATE_ONLY = re.compile(r'^(")?(\d{1,3}) (Zephyr|Phoenix|Scion|Colossus), (\d{3,4}) AE"?$')
EN_AE = re.compile(r"\b\d{1,4}\s*(?:AE|A\.E\.|BE|B\.E\.)\b")
ERA_WORDS = re.compile(r"\s*от\s+(?:Эпохи|Эры|Заката)\s+[А-ЯЁа-яё]+")
ERA_ABBR = re.compile(r"(?<=\d)\s*г\.\s*(?:В\.|Э\.\s*Д\.)")
ERA = re.compile(r"(?<=\d)(\s*)(?:AE|A\.E\.)\b")


def scrap_ru(m):
    day, mon, year = m.group(1), GEN[m.group(2)], m.group(3)
    if day:
        return "Похоже, это обрывок дневника, датированный %s %s %s года ПИ." % (day, mon, year)
    return ("Похоже, это обрывок дневника, датированный %s, %s год ПИ."
            % (INS[m.group(2)], year))


def fix_era(ru):
    out = ERA_WORDS.sub(" ПИ", ru)
    out = ERA_ABBR.sub(" ПИ", out)
    out = ERA.sub(r"\1ПИ", out)
    # только внутри строки-даты: два пробела подряд, появившиеся после вырезания
    out = out.replace("годом ПИ", "года ПИ")
    return re.sub(r"(?<=\d)  +(?=ПИ)", " ", out)


def main():
    apply = "--apply" in sys.argv
    fam = era = 0
    for fp in sorted(glob.glob("*/*.csv")):
        rows = list(csv.reader(io.open(fp, encoding="utf-8")))
        changed = 0
        for r in rows[1:]:
            if len(r) < 2 or not r[1].strip():
                continue
            m = SCRAP.match(r[0])
            d = DATE_ONLY.match(r[0].strip())
            if m:
                new = scrap_ru(m)
                kind = "семья"
            elif d:
                q = bool(d.group(1))
                new = "%s%s %s %s года ПИ%s" % ("«" if q else "", d.group(2),
                                                GEN[d.group(3)], d.group(4), "»" if q else "")
                kind = "строка-дата"
            elif EN_AE.search(r[0]):
                new = fix_era(r[1])
                kind = "эра"
            else:
                continue
            if new == r[1]:
                continue
            print("[%s] %s\n  --  %s\n  ++  %s" % (fp.replace(chr(92), "/"), kind, r[1][:110], new[:110]))
            r[1] = new
            changed += 1
            if kind in ("семья", "строка-дата"):
                fam += 1
            else:
                era += 1
        if changed and apply:
            with io.open(fp, "w", encoding="utf-8", newline="") as f:
                csv.writer(f, lineterminator="\n").writerows(rows)
    print("\nсемья обрывков дневника: %d | обозначение эры: %d%s"
          % (fam, era, " (записано)" if apply else " (план; для записи --apply)"))


main()
