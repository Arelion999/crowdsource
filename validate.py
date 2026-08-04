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
  - число %% не совпадает (одиночный % движок примет за начало спецификатора);
  - в переводе остался литерал [s] или [pl:"…"] (не преобразован в [форма1|форма2|форма3]);
  - несбалансированные скобки [ ] или теги <c>…</c>;
  - символ U+FFFD (битая кодировка);
  - потерян структурный префикс «Recipe[s]: », «Chest[s]: » и т.п. (в переводе нет двоеточия);
  - две формы в плюрал-группе, когда в оригинале [s]/[pl:…] — русскому нужно три (1 / 2-4 / 5+);
  - скобочная группа вообще без | при [s]/[pl:…] в оригинале («Ящик[и]» вместо
    «[Ящик|Ящика|Ящиков]») — в игре отрендерится литералом вместе со скобками;
  - потерян родовой маркер [M]/[F] в начале строки;
  - запрещённая GLOSSARY.md форма имени, когда оригинал именно про этот термин
    («Crystal Oasis» -> ~~Хрустальный Оазис~~); «хрустальная банка» не трогается;
  - «висячий» знак ударения (U+0301) не на гласной — след опечатки;
  - латинская буква-двойник внутри русского слова («Лом Пактa»);
  - потерян перенос строки: игра рисует строку так, как её разбил оригинал,
    и без переноса фразы склеиваются («…площадке.Примечание:»);
  - потеряна подпись письма («—Rytlock» после пустой строки);
  - краевые пробелы не совпали с оригиналом — ломается склейка «Рецепт: » + название;
  - невидимый символ, которого нет в оригинале (zero-width, мягкий перенос):
    принесён копипастой, ломает поиск по строке и подстановку имён.

ПРЕДУПРЕЖДЕНИЯ (не блокируют):
  - лишний перенос строки в переводе (чаще хвостовой);
  - потерян неразрывный пробел или маркер списка • — страдает вид, не смысл;
  - скобочная группа без | там, где в оригинале нет [s]/[pl:…];
  - английский Title Case, перенесённый в русский («Знамя Поиска Магии»);
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
ZW_CHARS = "​‌‍⁠﻿­"
# Пробельные символы, которые держат склейку. Перенос строки сюда НЕ входит:
# он про вёрстку и проверяется отдельно.
EDGE_WS = " \t             　"
NOSPACE = re.compile(r'\s+')
# Подпись письма: последняя строка вида «—Rytlock», «— Warmaster Jofast».
SIGNATURE = re.compile(r'\n[ \t]*[—–][ \t]*([^\n]{2,40})[ \t]*$')
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
# [M]/[F] — родовой маркер в начале строки, корпус его всегда сохраняет.
KNOWN_TOKENS = re.compile(r'\[(?:lbracket|rbracket|null|plur|nosep|topic-[fm]|f|an|the|[MF])\]|\[pl:"[^"]*"\]')
GENDER_MARK = re.compile(r'^\[([MF])\]')
# Слово перед группой, которое в группе же и повторено (см. проверку ниже).
DUP_WORD = re.compile(r'([А-Яа-яЁё-]{4,})\[([^\]\[]*\|[^\]\[]*)\]')

def strip_known(s):
    s = re.sub(r'%\w+%', '', s)
    s = re.sub(r'<[^>]+>', '', s)
    return KNOWN_TOKENS.sub('', s)

# Запрещённые формы имён берём прямо из GLOSSARY.md — из колонки «НЕ так» (~~форма~~).
# Правило привязано к оригиналу: ругаемся на «Хрустальный оазис» только когда в EN
# действительно Crystal Oasis, иначе поймали бы честную хрустальную банку.
def load_glossary_bans(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'GLOSSARY.md')
    rules = []
    try:
        lines = open(path, encoding='utf-8').read().splitlines()
    except OSError:
        return rules
    for line in lines:
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 3 or cells[0] in ('EN', '---'):
            continue
        ens = [re.sub(r'\(.*?\)', '', x).strip() for x in cells[0].split('/')]
        ens = [e for e in ens if re.fullmatch(r"[A-Za-z' ]{3,}", e)]
        bans = []
        for m in re.finditer(r'~~([^~]+)~~', cells[2]):
            bans += [x.strip() for x in m.group(1).split('/') if len(x.strip()) > 3]
        if ens and bans:
            en_re = re.compile('|'.join(r'\b' + re.escape(e) + r'\w{0,3}\b' for e in ens), re.I)
            # Если «НЕ так» отличается от канона только регистром («Дозор Духов» /
            # «Дозор духов») — правило про регистр, ищем точно. Иначе регистр не важен.
            ban_re = [(b, re.compile(r'(?<![А-Яа-яЁё])' + re.escape(b) + r'(?![А-Яа-яЁё])',
                                     0 if b.lower() == cells[1].lower() else re.I))
                      for b in bans]
            rules.append((en_re, cells[1], ban_re))
    return rules

GLOSSARY_BANS = load_glossary_bans()

# Русский текст не пишут английским Title Case («Знамя Поиска Магии»). Отличить
# перенесённую капитализацию от имени собственного по одной строке нельзя, поэтому
# спрашиваем у самого корпуса: если слово в середине фразы 160k строк почти всегда
# строчное — заглавная в нём подозрительна; имена собственные так защищены сами.
TITLE_CASE = re.compile(r'(?<=[а-яё] )[А-ЯЁ][а-яё]{2,}')
MID_WORD = re.compile(r'(?<=[а-яё] )[А-Яа-яЁё]{3,}')

# Число — единственная часть строки, которую перевод обязан повторить дословно, и
# единственный смысловой сдвиг, который вообще ловится машиной: «Contains 200» ->
# «Содержит 300» игрок видит как враньё. Разделитель тысяч нормализуем (1,400 =
# 1400), внутрь плейсхолдеров (%num1%) не лезем — там цифра часть имени.
# Цифра, приклеенная к букве, — часть имени, а не величина: FRU1TPUNCH, IIX0.
_NB = r'(?<![A-Za-z0-9])'
NUMBER = re.compile(_NB + r'\d{1,3}(?:[, ]\d{3})+(?![A-Za-z0-9])|'
                    + _NB + r'\d+(?![A-Za-z0-9])')

def numbers(s):
    """Числа строки. Плейсхолдеры и теги выкидываем: цифра в %num1% и <c=@a1> —
    часть имени токена, а не величина."""
    s = re.sub(r'%\w+%|<[^>]+>|\[pl:"[^"]*"\]', ' ', s)
    # «0330 hours» по-русски пишут «03:30» — это то же число, а не потеря.
    s = re.sub(r'(?<=\d):(?=\d)', '', s)
    # «120 x 240» и «120x240» — одно и то же, знак умножения не часть числа.
    s = re.sub(r'(?<=\d)\s*[xх×]\s*(?=\d)', ';', s)
    return {re.sub(r'[, ]', '', m.group()) for m in NUMBER.finditer(s)}

# По-русски число часто пишут словом — «season 1» -> «первого сезона», «1 chance» ->
# «один шанс». Это не потеря числа, поэтому слово засчитываем наравне с цифрой.
# Храним основы, а не полные формы: падежей и родов слишком много, основа однозначна.
WORD_NUM = {
    'один': '1', 'одна': '1', 'одно': '1', 'одну': '1', 'единствен': '1', 'перв': '1', 'раз-': '1', 'однажды': '1',
    'два': '2', 'две': '2', 'двух': '2', 'двум': '2', 'втор': '2', 'дважды': '2', 'оба': '2', 'обе': '2',
    'три': '3', 'трёх': '3', 'трех': '3', 'трём': '3', 'трет': '3', 'трижды': '3',
    'четыре': '4', 'четырёх': '4', 'четырех': '4', 'четверт': '4', 'четвёрт': '4', 'четырьмя': '4',
    'пять': '5', 'пяти': '5', 'пят': '5',
    'шесть': '6', 'шести': '6', 'шест': '6',
    'семь': '7', 'семи': '7', 'седьм': '7',
    'восемь': '8', 'восьми': '8', 'восьм': '8',
    'девять': '9', 'девяти': '9', 'девят': '9',
    'десять': '10', 'десяти': '10', 'десят': '10',
    'двадцат': '20', 'тридцат': '30', 'сорок': '40', 'пятьдесят': '50',
    'сто': '100', 'ста': '100', 'сот': '100', 'двест': '200', 'трист': '300',
    'тысяч': '1000', 'сотн': '100', 'полов': '2', 'дюжин': '12',
    'шестёр': '6', 'шестер': '6', 'семёр': '7', 'семер': '7', 'восьмёр': '8',
    'девятк': '9', 'десятк': '10', 'сороков': '40', 'пятидесят': '50',
}
WORD_NUM_RE = re.compile('|'.join(sorted(WORD_NUM, key=len, reverse=True)), re.I)

# Разряды по-русски отбивают пробелом («100 000»), по-английски запятой
# («100,000»). Пробел разрешаем только на стороне перевода: в оригинале
# «1 500-Point Essence» — это 1 и 500, а вовсе не 1500.
NUMBER_RU = re.compile(_NB + r'\d{1,3}(?:[,  ]\d{3})+(?![A-Za-z0-9])|'
                       + _NB + r'\d+(?![A-Za-z0-9])')

def numbers_ru(s):
    """Числа в переводе: цифрами (в том числе с пробелом в разрядах) и словом."""
    s = re.sub(r'%\w+%|<[^>]+>|\[pl:"[^"]*"\]', ' ', s)
    s = re.sub(r'(?<=\d):(?=\d)', '', s)
    s = re.sub(r'(?<=\d)\s*[xх×]\s*(?=\d)', ';', s)
    # Строгое (английское) прочтение тоже засчитываем: иначе на одинаковых
    # строках «76 554,326» стороны разошлись бы сами с собой.
    out = numbers(s)
    for m in NUMBER_RU.finditer(s):
        # «100 000» — это сто тысяч, а «31 254 13 56» — четыре числа подряд.
        # Отличить по виду нельзя, поэтому засчитываем оба прочтения: настоящую
        # подмену не спасёт ни одно из них, а ложную тревогу снимут оба.
        out.add(re.sub(r'[,  ]', '', m.group()))
        out |= set(re.findall(r'\d+', m.group()))
    return out | {WORD_NUM[m.group().lower()] for m in WORD_NUM_RE.finditer(s)}

LOWER_WORDS = set()          # заполняется в validate_paths, если корпус достаточно большой
TC_MIN_N, TC_MIN_SHARE, TC_MIN_ROWS = 12, 0.93, 20000

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
        # Три формы нужны именно там, где оригинал склоняет по числу: 1 / 2-4 / 5+.
        # Две формы — только для рода ([|а] в «готов[|а]»), там [s] в оригинале нет.
        plural_src = bool(PLURAL_EN.search(en))
        for grp in groups:
            if '|' in grp:
                if plural_src and grp.count('|') == 1:
                    errs.append(f"в группе две формы, для числа нужно три (1 / 2-4 / 5+): {grp}")
            elif plural_src:
                # «Ящик[и]» уедет в игру вместе со скобками — это не «подозрительно», это брак.
                errs.append(f"группа без | при [s]/[pl:…] в оригинале — нужны три формы: {grp}")
            else:
                warns.append(f"скобка без | : {grp}")
    if PREFIX_EN.match(en) and ':' not in ru:
        errs.append(f"потерян префикс «{PREFIX_EN.match(en).group().strip()}» — в переводе нет двоеточия")
    # «Лавровый лист[лист|листа|листов]» — движок склеит в «Лавровый листлист».
    # Формы задают либо окончанием («Ящик[|а|ов]»), либо словом целиком
    # («[Ящик|Ящика|Ящиков]»); машина смешала режимы и оставила слово снаружи.
    for m in DUP_WORD.finditer(rs):
        w, forms = m.group(1), m.group(2).split('|')
        if any(w[:4].lower() in f.lower() for f in forms):
            errs.append(f"слово «{w}» продублировано внутри группы: {m.group()[:40]}")
            break
    gm = GENDER_MARK.match(en)
    if gm and not ru.startswith(f'[{gm.group(1)}]'):
        errs.append(f"потерян родовой маркер [{gm.group(1)}] в начале строки")
    for en_re, canon, bans in GLOSSARY_BANS:
        if not en_re.search(en):
            continue
        for bad, bad_re in bans:
            if bad_re.search(ru):
                errs.append(f"форма «{bad}» запрещена глоссарием, канон — «{canon}»")
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
    # Процент в оригинале всегда экранирован как %%. Одиночный % в переводе движок
    # разберёт как начало спецификатора формата и съест соседний текст.
    if en.count('%%') != ru.count('%%'):
        errs.append(f"число %% не совпадает ({en.count('%%')} / {ru.count('%%')})")
    if LOWER_WORDS:
        tc = [w for w in TITLE_CASE.findall(ru) if w.lower() in LOWER_WORDS]
        if tc:
            warns.append(f"английский Title Case в русском: {' '.join(tc[:3])}")
    en_num = numbers(en)
    if en_num:
        ru_num = numbers_ru(ru)
        lost = en_num - ru_num
        if lost:
            extra = numbers(ru) - en_num
            # Подменой считаем только когда списки чисел выровнены — их поровну.
            # В длинном тексте с россыпью чисел «потерялось одно, зато есть другие»
            # ещё не значит, что одно подменили другим.
            if extra and len(NUMBER.findall(en)) == len(NUMBER_RU.findall(ru)):
                errs.append(f"число подменено: в оригинале {'/'.join(sorted(lost))}, "
                            f"в переводе {'/'.join(sorted(extra))}")
            else:
                # Потеря — не всегда брак: число пишут словом («шесть миллионов»),
                # римскими («XI веке»), или оно идиома («Golemancy 101» -> «для
                # начинающих»). Поэтому предупреждение: видно всё, гейт не встаёт.
                # Подмена выше — другое дело, там перевод просто врёт.
                warns.append(f"число оригинала потеряно: {'/'.join(sorted(lost))}")
    if '�' in ru:
        errs.append("символ U+FFFD (битая кодировка)")
    # --- служебные символы: перенос строки, края, невидимки, маркеры ---------
    # Перенос строки — часть вёрстки, а не оформления: игра рисует строку так,
    # как её разбил оригинал. Потерянный перенос склеивает фразы («…площадке.
    # Примечание:» без пробела), и это видно игроку.
    nl_en, nl_ru = en.count('\n'), ru.count('\n')
    if nl_en > nl_ru:
        if len(NOSPACE.sub('', ru)) < 0.55 * len(NOSPACE.sub('', en)):
            errs.append(f"потерян перенос строки и, похоже, целый сегмент "
                        f"({nl_en} -> {nl_ru}); перевод вдвое короче оригинала")
        else:
            errs.append(f"потерян перенос строки ({nl_en} -> {nl_ru})")
    elif nl_ru > nl_en:
        warns.append(f"лишний перенос строки ({nl_en} -> {nl_ru})")
    # Подпись письма стоит отдельной строкой после пустой; теряют её вместе с
    # переносами, а пропажу видно только по оригиналу.
    sig = SIGNATURE.search(en)
    if sig and not re.search(r'[—–]\s*\S', ru.strip()[-45:]):
        errs.append(f"потеряна подпись «{sig.group(1).strip()[:30]}»")
    # Краевой пробел оригинала — часть склейки («Рецепт: » + название). Лишний
    # краевой пробел — тоже брак: строка склеится с двойным.
    if en.strip(EDGE_WS)[:1] and (en[:len(en) - len(en.lstrip(EDGE_WS))]
                                  != ru[:len(ru) - len(ru.lstrip(EDGE_WS))]):
        errs.append("краевые пробелы в начале не совпадают с оригиналом")
    if en[len(en.rstrip(EDGE_WS)):] != ru[len(ru.rstrip(EDGE_WS)):]:
        errs.append("краевые пробелы в конце не совпадают с оригиналом")
    # Невидимки: в оригинале их нет, а в переводе есть — принесены копипастой
    # из вики и документов. Ломают поиск по строке и подстановку имён.
    for ch in ZW_CHARS:
        if ru.count(ch) > en.count(ch):
            errs.append(f"невидимый символ U+{ord(ch):04X}, которого нет в оригинале")
            break
    else:
        if ZW.search(ru):
            warns.append("zero-width символ")
    for ch in ' ⁠ ':
        if en.count(ch) > ru.count(ch):
            warns.append(f"потерян неразрывный символ U+{ord(ch):04X}")
            break
    if en.count('•') > ru.count('•'):
        warns.append("потерян маркер списка •")
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

def read_batch(fp):
    """Строки батча или None, если это не батч (у служебных таблиц другие колонки)."""
    rows = list(csv.reader(open(fp, encoding='utf-8')))
    if not rows or not rows[0] or rows[0][0].strip().lower() != 'english':
        return None
    return rows

def build_case_model(files):
    """Как корпус пишет слово в середине фразы: строчным или с заглавной."""
    stat, rows_seen = {}, 0
    for fp in files:
        try:
            rows = read_batch(fp)
        except Exception:
            continue
        if not rows:
            continue
        for r in rows[1:]:
            if len(r) < 2 or not r[1].strip():
                continue
            rows_seen += 1
            for w in MID_WORD.findall(r[1]):
                s = stat.setdefault(w.lower(), [0, 0])
                s[0 if w[0].islower() else 1] += 1
    if rows_seen < TC_MIN_ROWS:      # на одном файле модель недостоверна — проверку не включаем
        return set()
    return {w for w, (lo, up) in stat.items()
            if lo + up >= TC_MIN_N and lo / (lo + up) >= TC_MIN_SHARE}

def validate_paths(paths):
    """Возвращает (rows_checked, [(file,line,en,msg)] ошибок, [...] предупреждений)."""
    errors, warnings, checked = [], [], 0
    global LOWER_WORDS
    LOWER_WORDS = build_case_model(iter_csv(paths))
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
