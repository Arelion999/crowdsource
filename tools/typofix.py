#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Механические правки русской типографики в батчах.

Чинит только то, что решается без человека: правило однозначно, и результат не
зависит от смысла строки. Всё спорное («??» вместо выпавшего слова, непарные
кавычки, «юнит» и «хиты», которые надо переписывать словами) не трогается —
такие остаются в предупреждениях `validate.py` и ждут глаз.

    python tools/typofix.py                  # показать, что будет исправлено
    python tools/typofix.py apply            # записать
    python tools/typofix.py apply --only yo,punct
    python tools/typofix.py --show yo        # примеры правок класса

Классы:
  yo        — буква ё там, где написания через «е» не существует («еще» → «ещё»);
  quote-dot — точка из кавычек наружу («Иди.» → «Иди».);
  punct     — сочетания знаков к русскому набору («...?» → «?..», «!?» → «?!»);
  onomat    — звукоподражание через дефис («Ааа!» → «А-а-а!»);
  caps-vy   — «Вы» с прописной → со строчной (титулы не трогаются);
  god       — «Бог» в устойчивом обороте → со строчной;
  ui        — «кликните» → «щёлкните», «опцию» → «настройку»;
  lang      — название языка в меню выбора языка возвращается к оригиналу.

Английская колонка не меняется никогда: правки идут только во вторую колонку, и
файл переписывается лишь если в нём что-то поменялось.
"""
import argparse, csv, glob, os, re, sys, collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import validate as V

CROWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- ё ----------------------------------------------------------------------
# Каждое правило — то же слово, что ловит validate.YO, но с явно помеченной
# буквой: группа «e» стоит ровно на той «е», которая обязана быть «ё». Иначе
# место не вычислить — в «тяжелые» их две, а меняется первая.
YO_RULES = [
    r'(?<![а-яё])ещ(?P<e>е)(?![а-яё])',
    r'(?<![а-яё])(?:н)?е(?P<e>е)(?![а-яё])',
    r'(?<![а-яё])[а-яё]*йд(?P<e>е)(?:т(?:е|ся|сь)?|м|шь)(?![а-яё])',
    r'(?<![а-яё])[а-яё]*ш(?P<e>е)л(?![а-яё])',
    r'(?<![а-яё])[а-яё]*ь(?P<e>е)т(?:е|ся)?(?![а-яё])',
    r'(?<![а-яё])прид(?P<e>е)(?:т(?:е|ся)?|м|шь)(?![а-яё])',
    r'(?<![а-яё])(?:вед|нес|жив)(?P<e>е)(?:т|м)(?![а-яё])',
    r'(?<![а-яё])да(?P<e>е)т(?:е|ся)?(?![а-яё])',
    r'(?<![а-яё])(?:вез|вста|по)(?P<e>е)т(?![а-яё])',
    r'(?<![а-яё])сч(?P<e>е)т(?![а-яё])',
    r'(?<![а-яё])сч(?P<e>е)тчик[а-яё]*(?![а-яё])',
    r'(?<![а-яё])(?:отч|зач|расч|уч|подсч|поч)(?P<e>е)т[а-яё]*(?![а-яё])',
    r'(?<![а-яё])пол(?P<e>е)т(?:а|е|ом|ы|ов|ам|ами|ах)?(?![а-яё])',
    r'(?<![а-яё])впер(?P<e>е)д(?![а-яё])',
    r'(?<![а-яё])(?:объ|при|подъ)(?P<e>е)м(?:а|е|ом|ы|ов|ам|ами|ах)?(?![а-яё])',
    r'(?<![а-яё])(?:объ|подъ)(?P<e>е)мн[а-яё]*(?![а-яё])',
    r'(?<![а-яё])при(?P<e>е)мник[а-яё]*(?![а-яё])',
    r'(?<![а-яё])ч(?P<e>е)рн(?:ый|ая|ое|ые|ого|ому|ым|ом|ых|ыми|ой|ую|о)(?![а-яё])',
    r'(?<![а-яё])ж(?P<e>е)лт(?:ый|ая|ое|ые|ого|ому|ым|ом|ых|ыми|ой|ую|о)(?![а-яё])',
    r'(?<![а-яё])зел(?P<e>е)н(?:ый|ая|ое|ые|ого|ому|ым|ом|ых|ыми|ой|ую|о)(?![а-яё])',
    r'(?<![а-яё])т(?P<e>е)мн(?:ый|ая|ое|ые|ого|ому|ым|ом|ых|ыми|ой|ую|о)(?![а-яё])',
    r'(?<![а-яё])т(?P<e>е)пл(?:ый|ая|ое|ые|ого|ому|ым|ом|ых|ыми|ой|ую)(?![а-яё])',
    r'(?<![а-яё])л(?P<e>е)гк(?:ий|ая|ое|ие|ого|ому|им|ом|их|ими|ой|ую|о|ость[а-яё]*)(?![а-яё])',
    r'(?<![а-яё])тяж(?P<e>е)л(?:ый|ая|ое|ые|ого|ому|ым|ом|ых|ыми|ой|ую|о)(?![а-яё])',
    r'(?<![а-яё])м(?P<e>е)ртв(?:ый|ая|ое|ые|ого|ому|ым|ом|ых|ыми|ой|ую)(?![а-яё])',
    r'(?<![а-яё])серь(?P<e>е)зн[а-яё]*(?![а-яё])',
    r'(?<![а-яё])над(?P<e>е)жн[а-яё]*(?![а-яё])',
    r'(?<![а-яё])никч(?P<e>е)мн[а-яё]*(?![а-яё])',
    r'(?<![а-яё])ч(?P<e>е)тк[а-яё]*(?![а-яё])',
    r'(?<![а-яё])зв(?P<e>е)здн[а-яё]*(?![а-яё])',
    r'(?<![а-яё])щ(?P<e>е)лк(?:ните|нуть|нув|ает|аете|ают|ая|ай|айте|нул[а-яё]*)(?![а-яё])',
    r'(?<![а-яё])сест(?P<e>е)р(?![а-яё])',
    r'(?<![а-яё])остри(?P<e>е)(?![а-яё])',
    r'(?<![а-яё])(?:копь|ружь)(?P<e>е)м?(?![а-яё])',
    r'(?<![а-яё])(?P<e>е)жик[а-яё]*(?![а-яё])',
    r'(?<![а-яё])(?P<e>е)лк(?:а|и|у|ой|ам|ами|ах)(?![а-яё])',
    r'(?<![а-яё])бер(?P<e>е)з(?:а|ы|е|у|ой|ки|кой)(?![а-яё])',
]
YO_RULES = [re.compile(r, re.I) for r in YO_RULES]

def _yo_sub(m):
    """Меняет помеченную «е» на «ё», сохраняя регистр слова целиком."""
    i = m.start('e') - m.start()
    s = m.group()
    return s[:i] + ('Ё' if s[i] == 'Е' else 'ё') + s[i + 1:]

def fix_yo(en, ru):
    for r in YO_RULES:
        ru = r.sub(_yo_sub, ru)
    return ru

# --- точка из кавычек -------------------------------------------------------
# Только типографские кавычки: у прямой « " » по одному виду не понять, она
# закрывающая или открывающая, и точка может оказаться не на своём месте.
def fix_quote_dot(en, ru):
    # Проход повторяется: во вложенных кавычках («…текст.»») перенос точки наружу
    # открывает следующую такую же пару, а re.sub продолжает уже за заменой.
    for _ in range(4):
        new = re.sub(r'(?<![.!?…])\.([»”])', r'\1.', ru)
        if new == ru:
            break
        ru = new
    return ru

# --- сочетания знаков -------------------------------------------------------
# Меняем только то, где русский вариант однозначен. «??», «???», «???!» не
# трогаем: это чаще след выпавшего слова (машинный перевод так метит потерю),
# и «?» вместо них соврёт больше, чем оставленный мусор.
# «!.» и «.!» сюда не входят: убрать одну из точек — значит решить, что здесь
# конец фразы, а что часть названия («Hammerhart Rumble!»), и `charscan` считает
# такую потерю знака конца регрессией. Это работа переводчика.
PUNCT_MAP = {'...?': '?..', '...!': '!..', '!...': '!..', '?...': '?..', '...?!': '?!.',
             '!?': '?!', '?!?': '?!', '?!?!': '?!'}

def fix_punct(en, ru):
    out, last = [], 0
    for m in V.PUNCT_RUN.finditer(ru):
        # Многоточие одним символом не трогаем: развернув «!…» в «!..», мы теряем
        # сам символ, а его пропажу словарь считает отдельным дефектом.
        if '…' in m.group():
            continue
        run = m.group()
        if run in V.PUNCT_OK or V.PUNCT_RANGE.search(ru[max(0, m.start() - 1):m.end() + 1]):
            continue
        if run[0] == '.' and V.ABBREV.search(ru[:m.start()]):
            continue
        rep = '...' if set(run) == {'.'} else PUNCT_MAP.get(run)
        if not rep:
            continue
        out.append(ru[last:m.start()]); out.append(rep); last = m.end()
    return ''.join(out) + ru[last:] if out else ru

# --- звукоподражания --------------------------------------------------------
# Регистр каждой буквы сохраняем: «Ааа» → «А-а-а», «АААА» → «А-А-А-А». Правило
# просит разделить одинаковые буквы дефисом, а не переписать крик строчными.
def fix_onomat(en, ru):
    return V.ONOMAT.sub(lambda m: '-'.join(m.group()), ru)

# --- «Вы» с прописной -------------------------------------------------------
def fix_caps_vy(en, ru):
    out, last = [], 0
    for m in V.CAP_VY.finditer(ru):
        if re.match(r'\s*[А-ЯЁ]', ru[m.end():]):     # «Ваше Величество» — титул
            continue
        out.append(ru[last:m.start()]); out.append(m.group().lower()); last = m.end()
    return ''.join(out) + ru[last:] if out else ru

# --- «Бог» в устойчивом обороте --------------------------------------------
def fix_god(en, ru):
    return V.GOD_LOWER.sub(lambda m: m.group().replace('Бог', 'бог').replace('Боже', 'боже')
                           .replace('Господи', 'господи'), ru)

# --- стандарты интерфейса ---------------------------------------------------
# Глагол «кликнуть» заменяется на «щёлкнуть» той же формы, существительное
# «клик» — на «щелчок» в том же падеже. «Юнит» и «хиты» сюда не входят: там
# меняется не слово, а вся фраза, и это работа переводчика.
CLICK_V = re.compile(r'(?<![а-яё])[Кк]лик(ните|нуть|нув|нул[а-яё]*|ает|аете|ают|ая|ай|айте|аем)(?![а-яё])')
CLICK_N = {'клик': 'щелчок', 'клика': 'щелчка', 'клику': 'щелчку', 'кликом': 'щелчком',
           'клике': 'щелчке', 'клики': 'щелчки', 'кликов': 'щелчков', 'кликам': 'щелчкам',
           'кликами': 'щелчками', 'кликах': 'щелчках'}
CLICK_N_RE = re.compile(r'(?<![а-яё])(' + '|'.join(CLICK_N) + r')(?![а-яё])', re.I)
OPT = {'опция': 'настройка', 'опцию': 'настройку', 'опцией': 'настройкой',
       'опций': 'настроек', 'опциям': 'настройкам', 'опциями': 'настройками',
       'опциях': 'настройках'}
OPT_RE = re.compile(r'(?<![а-яё])(' + '|'.join(OPT) + r')(?![а-яё])', re.I)
EN_OPT = re.compile(r'\boptions?\b', re.I)

def _keep_case(src, dst):
    return dst.capitalize() if src[:1].isupper() else dst

def fix_ui(en, ru):
    ru = CLICK_V.sub(lambda m: _keep_case(m.group(), 'щёлк' + m.group(1)), ru)
    ru = CLICK_N_RE.sub(lambda m: _keep_case(m.group(), CLICK_N[m.group().lower()]), ru)
    if EN_OPT.search(en):
        ru = OPT_RE.sub(lambda m: _keep_case(m.group(), OPT[m.group().lower()]), ru)
    return ru

# --- меню выбора языка ------------------------------------------------------
def fix_lang(en, ru):
    return en if en.strip().lower() in V.LANG_MENU else ru

FIXERS = [('yo', fix_yo), ('quote-dot', fix_quote_dot), ('punct', fix_punct),
          ('onomat', fix_onomat), ('caps-vy', fix_caps_vy), ('god', fix_god),
          ('ui', fix_ui), ('lang', fix_lang)]

def batches(root):
    return sorted(glob.glob(os.path.join(root, '**', '*.csv'), recursive=True))

def run(root, apply, only, show):
    stat = collections.Counter()
    samples = collections.defaultdict(list)
    files_changed = 0
    for fp in batches(root):
        try:
            with open(fp, 'rb') as f:
                bom = f.read(3) == b'\xef\xbb\xbf'
            with open(fp, encoding='utf-8-sig', newline='') as f:
                rows = list(csv.reader(f))
        except Exception:
            continue
        # Батч или таблица слоя имён: первые две колонки — «english,translate»,
        # дальше может идти служебное (`pn_harvest.csv` несёт ещё четыре). Файлы
        # с другой шапкой (выгрузки API, отчёты синка) не наши.
        if not rows or [c.strip().lower() for c in rows[0][:2]] != ['english', 'translate']:
            continue
        touched = False
        for r in rows[1:]:
            if len(r) < 2 or not r[1].strip():
                continue
            ru = r[1]
            for name, fn in FIXERS:
                if only and name not in only:
                    continue
                new = fn(r[0], ru)
                if new != ru:
                    stat[name] += 1
                    if len(samples[name]) < 8:
                        samples[name].append((ru[:70], new[:70]))
                    ru = new
            if ru != r[1]:
                r[1] = ru
                touched = True
        if touched:
            files_changed += 1
            if apply:
                # BOM возвращаем, если он был: по нему часть инструментов отличает
                # служебные таблицы от батчей, и снятый BOM меняет их видимость.
                with open(fp, 'w', encoding='utf-8-sig' if bom else 'utf-8', newline='') as f:
                    csv.writer(f, lineterminator='\n').writerows(rows)
    print(('ИСПРАВЛЕНО' if apply else 'БУДЕТ ИСПРАВЛЕНО') + f" (файлов: {files_changed})")
    for name, _ in FIXERS:
        if stat[name]:
            print(f"  {stat[name]:6,}  {name}")
    for name in (show or []):
        print(f"\n### {name}")
        for a, b in samples.get(name, []):
            print(f"   было: {a}\n   стало: {b}")
    return stat

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('action', nargs='?', default='dry', choices=['dry', 'apply'])
    ap.add_argument('--only', help='классы через запятую')
    ap.add_argument('--show', help='показать примеры правок этих классов')
    ap.add_argument('--root', default=CROWD)
    a = ap.parse_args()
    only = set(a.only.split(',')) if a.only else None
    show = a.show.split(',') if a.show else []
    if only:
        unknown = only - {n for n, _ in FIXERS}
        if unknown:
            sys.exit(f"неизвестный класс: {', '.join(sorted(unknown))}")
    run(a.root, a.action == 'apply', only, show)

if __name__ == '__main__':
    main()
