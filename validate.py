#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Линтер батчей перевода: ловит поломки ДО вливания в игру.

Запуск:
    python validate.py                 # проверить все батчи crowdsource/*/*.csv
    python validate.py new/new_001.csv # конкретный файл/папку
    python validate.py --warnings      # показать и предупреждения (по умолчанию только ошибки)

Проверяет только НЕпустые переводы. Коды выхода: 0 — ошибок нет, 1 — есть ошибки.

ОШИБКИ (блокируют релиз):
  - набор плейсхолдеров/тегов (%str1%, <c=…>, [lbracket]/[rbracket]/[null]) не совпадает с оригиналом;
  - число %% не совпадает;
  - в переводе остался литерал [s] или [pl:"…"] (не преобразован в [форма1|форма2|форма3]);
  - несбалансированные скобки [ ] или теги <c>…</c>;
  - символ U+FFFD (битая кодировка).

ПРЕДУПРЕЖДЕНИЯ (не блокируют):
  - скобочная группа без | (подозрительно для плюрала);
  - zero-width символы;
  - перевод дословно равен английскому предложению (возможно, не переведено).
"""
import csv, glob, os, re, sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOK = re.compile(r'%\w+%|<[^>]+>|\[lbracket\]|\[rbracket\]|\[null\]')
CYR = re.compile(r'[а-яёА-ЯЁ]')
LEFTOVER = re.compile(r'\[s\]|\[pl:')
ZW = re.compile(r'[​‌‍﻿]')

def tokens(s):
    return sorted(TOK.findall(s))

def strip_known(s):
    s = re.sub(r'%\w+%', '', s)
    s = re.sub(r'<[^>]+>', '', s)
    return s.replace('[lbracket]', '').replace('[rbracket]', '').replace('[null]', '')

def check_row(en, ru):
    errs, warns = [], []
    if tokens(en) != tokens(ru):
        errs.append("плейсхолдеры/теги не совпадают с оригиналом")
    if LEFTOVER.search(ru):
        errs.append("в переводе остался [s]/[pl:…]")
    rs = strip_known(ru)
    if rs.count('[') != rs.count(']'):
        errs.append("несбалансированные скобки [ ]")
    else:
        for grp in re.findall(r'\[[^\]]*\]', rs):
            if '|' not in grp:
                warns.append(f"скобка без | : {grp}")
    if en.count('%%') != ru.count('%%'):
        warns.append(f"число %% не совпадает ({en.count('%%')} / {ru.count('%%')})")
    if '�' in ru:
        errs.append("символ U+FFFD (битая кодировка)")
    if ZW.search(ru):
        warns.append("zero-width символ")
    if ru.strip() == en.strip() and not CYR.search(en) and re.search(r'[.!?]', en) and len(en) >= 15:
        warns.append("перевод == оригиналу (возможно, не переведено)")
    return errs, warns

def iter_csv(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += glob.glob(os.path.join(p, '**', '*.csv'), recursive=True)
        elif p.endswith('.csv'):
            files.append(p)
    return sorted(set(files))

def validate_paths(paths):
    """Возвращает (rows_checked, [(file,line,en,msg)] ошибок, [...] предупреждений)."""
    errors, warnings, checked = [], [], 0
    for fp in iter_csv(paths):
        try:
            rows = list(csv.reader(open(fp, encoding='utf-8')))
        except Exception as e:
            errors.append((fp, 0, '', f"не читается: {e}")); continue
        for i, r in enumerate(rows[1:], start=2):  # line number in file
            if len(r) < 2 or not r[1].strip():
                continue
            checked += 1
            e, w = check_row(r[0], r[1])
            rel = os.path.relpath(fp)
            for m in e: errors.append((rel, i, r[0][:60], m))
            for m in w: warnings.append((rel, i, r[0][:60], m))
    return checked, errors, warnings

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    show_warn = '--warnings' in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    paths = args or [here]
    checked, errors, warnings = validate_paths(paths)
    print(f"Проверено переведённых строк: {checked:,} | ошибок: {len(errors)} | предупреждений: {len(warnings)}")
    for f, ln, en, m in errors[:200]:
        print(f"  ОШИБКА  {f}:{ln}  «{en}» — {m}")
    if len(errors) > 200:
        print(f"  … и ещё {len(errors)-200} ошибок")
    if show_warn:
        for f, ln, en, m in warnings[:200]:
            print(f"  warn    {f}:{ln}  «{en}» — {m}")
        if len(warnings) > 200:
            print(f"  … и ещё {len(warnings)-200} предупреждений")
    sys.exit(1 if errors else 0)

if __name__ == '__main__':
    main()
