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
  - символ U+FFFD (битая кодировка);
  - потерян структурный префикс «Recipe[s]: », «Chest[s]: » и т.п. (в переводе нет двоеточия);
  - две формы в плюрал-группе, когда в оригинале [s]/[pl:…] — русскому нужно три (1 / 2-4 / 5+);
  - «висячий» знак ударения (U+0301) не на гласной — след опечатки;
  - латинская буква-двойник внутри русского слова («Лом Пактa»).

ПРЕДУПРЕЖДЕНИЯ (не блокируют):
  - скобочная группа без | (подозрительно для плюрала);
  - zero-width символы;
  - перевод дословно равен английскому предложению (возможно, не переведено);
  - «ты» и «вы» в одной строке (разнобой обращения);
  - оригинал кончается на .!? — перевод нет (потерянная точка);
  - пробел перед знаком препинания.
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
PLURAL_EN = re.compile(r'\[s\]|\[pl:')
# «Recipe[s]: », «Trophy: » — структурный префикс имени предмета. Если в переводе
# нет двоеточия, слово-префикс просто потеряли (так new_023 лишился всех «Рецепт»).
PREFIX_EN = re.compile(r'^[A-Z][A-Za-z\' -]{2,30}(?:\[s\]|\[pl:"[^"]*"\]):\s')
# Знак ударения ставится на гласную; на согласной/кавычке — мусор от набора.
STRESS_OK = re.compile(r'[аеёиоуыэюяАЕЁИОУЫЭЮЯ]́')
STRESS_ANY = re.compile(r'[̀-ͯ]')
TY = re.compile(r'\b(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоего|твоей|твоих|твою|твоим|твоём)\b', re.I)
# Латинская буква-двойник внутри русского слова («Лом Пактa» — последняя 'a' латинская).
# Глазом не видно, а слово уже не русское: ломается поиск и подстановка имён.
# Ошибкой считаем только два случая — буква зажата кириллицей («кастoранской») или
# строчная буква в конце русского слова («Пактa»). Заглавная латиница после кириллицы
# обычно склеенное сокращение («ДеббиP.S.»), а латиница рядом с латиницей — имя («Kye»).
HOMO = set('aceopxyABCEHKMOPTX')

def homoglyph(s):
    def cyr(ch):
        return bool(ch) and ('а' <= ch.lower() <= 'я' or ch.lower() == 'ё')
    for i, ch in enumerate(s):
        if ch not in HOMO or not cyr(s[i-1] if i else ''):
            continue
        nxt = s[i+1] if i + 1 < len(s) else ''
        if cyr(nxt) or (ch.islower() and not (nxt.isascii() and nxt.isalpha())):
            return s[max(0, i-8):i+8]
    return None
VY = re.compile(r'\b(вы|вас|вам|вами|ваш|ваша|ваше|ваши|вашего|вашей|ваших|вашу|вашим|вашем)\b', re.I)

def tokens(s):
    return sorted(TOK.findall(s))

# Служебные токены движка в квадратных скобках — это НЕ плюрал-группы [a|b|c],
# поэтому перед проверкой скобок их убираем (иначе ложное «скобка без |»).
KNOWN_TOKENS = re.compile(r'\[(?:lbracket|rbracket|null|plur|nosep|topic-[fm]|f|an|the)\]|\[pl:"[^"]*"\]')

def strip_known(s):
    s = re.sub(r'%\w+%', '', s)
    s = re.sub(r'<[^>]+>', '', s)
    return KNOWN_TOKENS.sub('', s)

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
        groups = re.findall(r'\[[^\]]*\]', rs)
        for grp in groups:
            if '|' not in grp:
                warns.append(f"скобка без | : {grp}")
        # Три формы нужны именно там, где оригинал склоняет по числу: 1 / 2-4 / 5+.
        # Две формы — только для рода ([|а] в «готов[|а]»), там [s] в оригинале нет.
        if PLURAL_EN.search(en):
            for grp in groups:
                if grp.count('|') == 1:
                    errs.append(f"в группе две формы, для числа нужно три (1 / 2-4 / 5+): {grp}")
    if PREFIX_EN.match(en) and ':' not in ru:
        errs.append(f"потерян префикс «{PREFIX_EN.match(en).group().strip()}» — в переводе нет двоеточия")
    if STRESS_ANY.search(STRESS_OK.sub('', ru)):
        errs.append("знак ударения не на гласной (мусор от набора)")
    hg = homoglyph(ru)
    if hg:
        errs.append(f"латинская буква внутри русского слова: «{hg}»")
    if TY.search(ru) and VY.search(ru):
        warns.append("«ты» и «вы» в одной строке")
    if en.rstrip()[-1:] in '.!?' and ru.rstrip()[-1:] not in '.!?…»"\')%]>':
        warns.append("оригинал кончается точкой/!/?, перевод — нет")
    if re.search(r'\s[,.;:!?](?=\s|$)', ru):
        warns.append("пробел перед знаком препинания")
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
        # Батч — это CSV с заголовком «english,translate». Служебные таблицы
        # (отчёты синка, sync/reports/*.csv) пропускаем: у них другие колонки.
        if not rows or not rows[0] or rows[0][0].strip().lower() != 'english':
            continue
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
