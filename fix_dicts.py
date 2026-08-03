#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Чинит механические поломки переводов в игровых словарях glyphCore/ (dict_*.csv,
main_strings.csv) — те, что находит validate.py, но которые линтер по батчам
никогда не видел: игровые файлы им не проверялись.

    python fix_dicts.py                 # ПОКАЗАТЬ, что будет исправлено (ничего не пишет)
    python fix_dicts.py --apply         # исправить
    python fix_dicts.py --apply wrap br # только выбранные правила
    python fix_dicts.py --examples      # примеры правок по каждому правилу

Правила (каждое — отдельный класс поломки):
  wrap      перевод потерял обёртку <c=@flavor>…</c> / <center>…</center> целиком
            (текст теряет цвет/центрирование) — обёртка возвращается;
  br        <br> заменён живым переводом строки или склеен с авторской подписью;
  brackets  [lbracket]/[rbracket] заменены живыми скобками — движок примет такую
            скобку за плюрал-группу;
  num       в переводе %num1%, хотя в оригинале живое число — игрок увидит «%num1%»;
  counter   то же для счётчиков события («Hunters alive: 3»);
  lostnum   перевод потерял %num2% — на его месте зияет двойной пробел;
  homoglyph латинская буква внутри русского слова («Лом Пактa»);
  extratag  в переводе появился <c=…>, которого в оригинале нет;
  manual    точечные правки по списку ниже (плюрал-группы, [null], мусорные строки).

ГЛАВНАЯ СТРАХОВКА: любая правка принимается, только если после неё validate.check_row
даёт МЕНЬШЕ ошибок и не появляется новых. Плюс round-trip: файл переписывается лишь
если пересериализация без изменений даёт байт-в-байт исходные байты (как в merge_back).
"""
import csv, io, os, re, sys, glob, collections

HERE  = os.path.dirname(os.path.abspath(__file__))
GCDIR = os.path.dirname(HERE)

sys.path.insert(0, HERE)
import validate                                            # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
csv.field_size_limit(1 << 30)

TOK     = re.compile(r'%\w+%|<[^>]+>|\[lbracket\]|\[rbracket\]|\[null\]')
NUM_TOK = re.compile(r'%num\d+%')
EN_NUM  = re.compile(r'(?<![%\w])\d+(?![%\w])')
WRAP    = re.compile(r'^(<(?:c=[^>]+|center)>)(.*)(</(?:c|center)>)$', re.S)

# Точечные правки: (условие по english, что искать в переводе, чем заменить, зачем).
# Применяются во всех файлах сразу — одна и та же строка живёт и в main_strings.csv,
# и в профильном словаре.
MANUAL = [
    ('renown heart[s]', 'сердце[s]', 'сердц[е|а|ец]',
     'в переводе остался литерал [s]'),
    ('boss[pl:"bosses"]', 'босс[а|ов]', 'босс[|а|ов]',
     'две формы вместо трёх: 1 босс / 2 босса / 5 боссов'),
    ('opponent[s]', 'противник[а|ов]', 'противник[|а|ов]',
     'две формы вместо трёх'),
    ('Son[s] of Svanir', 'Сын[а|ов]', 'Сын[|а|ов]',
     'две формы вместо трёх'),
    ("Subdirector NULL", 'Subdirector [null]', 'Subdirector NULL',
     '[null] — служебный токен движка, а тут имя голема NULL'),
    ('capacity—at—null', '—[null].', '—нуле.',
     '[null] вместо слова «нуль»'),
    ('Full Counter refreshes', 'при попадании.<c=', 'при попадании.\n<c=',
     'потерян перенос строки перед подсказкой'),
    ('Full Counter refreshes', 'и страх.]', 'и страх.',
     'лишняя закрывающая скобка в конце'),
    ('Assist the Sentinels patrolling', ']nСпасибо.',
     'Помогите Стражам, патрулирующим Crystalwept Groves.',
     'мусор вместо перевода (Sentinels → Стражи по корпусу)'),
]


def TOKS(s):
    return TOK.findall(s)


def lower_first(s):
    return s[:1].lower() + s[1:] if s[:1].isupper() else s


def first_form(s):
    """«[Витиеватая инсигния|…|…] Прорицателя» -> «Витиеватая инсигния Прорицателя»."""
    m = re.match(r'^\[([^\]|]+)\|[^\]]*\](.*)$', s, re.S)
    return (m.group(1) + m.group(2)) if m else s


def rule_wrap(en, ru):
    """Обёртка целиком: EN = «<c=@flavor>весь текст</c>», перевод потерял оба тега.

    Строго один открывающий и один закрывающий тег на строку: в «<c=@abilitytype>Ambush.</c>
    Release…» цветом выделено только первое слово, и обернуть в него весь перевод нельзя."""
    m = WRAP.match(en.rstrip())
    if not m or '<' in ru or '>' in ru:
        return None
    tags = [t for t in TOKS(en) if t.startswith('<')]
    if len(tags) != 2 or tags[1] not in ('</c>', '</center>'):
        return None
    return m.group(1) + ru + m.group(3)


def rule_br(en, ru):
    if '<br>' not in en or '<br>' in ru:
        return None
    # 1) перевод строки вместо <br>
    if '\n' in ru and en.count('<br>') == ru.count('\n') and '\n' not in en:
        return ru.replace('\n', '<br>')
    # 2) авторская подпись: EN «…"<br>—Имя», RU «…»— Имя»
    if '<br>—' in en and en.count('<br>') == 1 and '\n' not in ru:
        for pat in ('»—', '».—', '»  —', '» —'):
            if ru.count(pat) == 1:
                return ru.replace(pat, '»<br>—')
    return None


def rule_brackets(en, ru):
    if '[lbracket]' not in en or '[lbracket]' in ru:
        return None
    m = re.match(r'^\[([^\[\]]+)\]', ru)
    if not m:
        return None
    return f'[lbracket]{m.group(1)}[rbracket]' + ru[m.end():]


def rule_num(en, ru):
    """%num1% -> живое число из оригинала.

    Осторожно с падежом: переводчик писал форму под «%num1%», то есть родительный
    множественного («%num1% врагов»). Она верна только для чисел на 0 и 5–9 и для
    11–14: «30 врагов» ✅, а «3 карт» ❌ — там нужно «3 карты», это уже не механика.
    Поэтому подставляем только числа с подходящим окончанием."""
    if not NUM_TOK.search(ru) or NUM_TOK.search(en) or '  ' in en:
        return None
    nums = EN_NUM.findall(en)
    if len(nums) != 1 or len(NUM_TOK.findall(ru)) != 1:
        return None
    n = nums[0]
    if n in ru:                      # число уже есть в переводе — %num1% про другое
        return None
    v = int(n)
    if not (v % 100 in range(11, 15) or v % 10 in (0, 5, 6, 7, 8, 9)):
        return None
    return NUM_TOK.sub(n, ru)


def rule_extratag(en, ru):
    if '<' in en or '<c=' not in ru:
        return None
    return re.sub(r'</?c(?:=[^>]*)?>', '', ru).strip()


def rule_brtag(en, ru):
    """EN «…текст.<br><c=@reminder>подсказка», RU склеил абзацы без <br>."""
    if '<br><c=' not in en or '<br>' in ru:
        return None
    m = re.search(r'<br>(<c=[^>]+>)', en)
    if not m or ru.count(m.group(1)) != 1:
        return None
    return ru.replace(m.group(1), '<br>' + m.group(1))


def rule_pseudotag(en, ru):
    """«<Veteran/Champion> …» -> в переводе тег перевели («<Ветеран/Чемпион>»).
    Движок подставляет в такой тег своё значение — внутрь лезть нельзя."""
    te, tr = TOKS(en), TOKS(ru)
    miss = [t for t in te if t not in tr and t.startswith('<')]
    extra = [t for t in tr if t not in te and t.startswith('<')]
    if len(miss) != 1 or len(extra) != 1:
        return None
    return ru.replace(extra[0], miss[0])


def rule_counter(en, ru):
    """Счётчик события: EN «Hunters alive: 3», RU «Охотники живы: %num1%» —
    игрок увидит «%num1%». В конце строки число/таймер, падеж не меняется."""
    if NUM_TOK.search(en) or not NUM_TOK.search(ru) or ' x of ' in en or ' x из ' in ru:
        return None
    m = re.search(r'(\d+(?::\d+)?)\s*$', en.rstrip())
    if not m:
        return None
    tail = m.group(1)
    for pat in ('%num1%:%num2%', '%num1%'):
        if ru.rstrip().endswith(pat) and len(NUM_TOK.findall(ru)) == pat.count('%num'):
            return ru.rstrip()[:-len(pat)] + tail
    return None


def rule_lostnum(en, ru):
    """Перевод потерял %num2% — на его месте зияет двойной пробел:
    EN «Open %num2% Glitched Chests», RU «Откройте  Glitched Chests»."""
    miss = [t for t in TOKS(en) if t.startswith('%') and t not in TOKS(ru)]
    if len(miss) != 1 or ru.count('  ') != 1 or re.search(r'\S  +\S', en):
        return None
    return ru.replace('  ', f' {miss[0]} ', 1)


HOMO = {'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у',
        'A': 'А', 'B': 'В', 'C': 'С', 'E': 'Е', 'H': 'Н', 'K': 'К', 'M': 'М',
        'O': 'О', 'P': 'Р', 'T': 'Т', 'X': 'Х'}
def is_cyr(ch):
    return bool(ch) and ('а' <= ch.lower() <= 'я' or ch.lower() == 'ё')


def homoglyph_hits(s):
    """Позиции латинских букв-двойников, стоящих ВНУТРИ русского слова.

    Считаем ошибкой только два случая:
      * буква зажата кириллицей с обеих сторон — «кастoранской»;
      * строчная буква в конце русского слова — «Лом Пактa», «сунy-ка».
    Заглавная латиница после кириллицы — обычно склеенное сокращение («ДеббиP.S.»,
    «CCУВЕДОМЛЕНИЕ»), а латиница рядом с латиницей — просто имя («министр Kye»)."""
    hits = []
    for i, ch in enumerate(s):
        if ch not in HOMO:
            continue
        prev = s[i - 1] if i else ''
        nxt = s[i + 1] if i + 1 < len(s) else ''
        if not is_cyr(prev):
            continue
        if is_cyr(nxt) or (ch.islower() and not (nxt.isascii() and nxt.isalpha())):
            hits.append(i)
    return hits


def rule_homoglyph(en, ru):
    hits = homoglyph_hits(ru)
    if not hits:
        return None
    out = list(ru)
    for i in hits:
        out[i] = HOMO[out[i]]
    return ''.join(out)


def rule_manual(en, ru):
    out = ru
    for en_key, find, repl, _why in MANUAL:
        if en_key in en and find in out:
            out = out.replace(find, repl)
    if out != ru:
        return out
    # «Recipe[s]: X» -> «Рецепт[|а|ов]: x» (структурный префикс имени предмета)
    if en.startswith('Recipe[s]: ') and ':' not in ru:
        return 'Рецепт[|а|ов]: ' + lower_first(first_form(ru))
    return None


RULES = [('wrap', rule_wrap), ('br', rule_br), ('brtag', rule_brtag),
         ('pseudotag', rule_pseudotag), ('brackets', rule_brackets),
         ('num', rule_num), ('counter', rule_counter), ('lostnum', rule_lostnum),
         ('extratag', rule_extratag), ('manual', rule_manual)]

# Правила, которые линтер не ловит: применяются ко всем строкам, а не только
# к «битым». Принимаются, если счёт ошибок от них не ухудшился.
COSMETIC = [('homoglyph', rule_homoglyph)]


def score(en, ru):
    """(сколько ошибок линтера, насколько разошлись наборы токенов). Чем меньше — тем
    лучше. Второй член нужен, чтобы принимать частичные правки: строка, где потеряны
    и обёртка <c=…>, и <br>, чинится двумя правилами по очереди, а сообщение линтера
    у неё всё это время одно и то же."""
    errs = set(validate.check_row(en, ru)[0])
    te, tr = collections.Counter(TOKS(en)), collections.Counter(TOKS(ru))
    return errs, len(errs), sum((te - tr).values()) + sum((tr - te).values())


def fix_row(en, ru, enabled):
    """Возвращает (новый перевод, [имена сработавших правил]) или None.

    Правила прогоняются по кругу, пока строка улучшается. Правка принимается, только
    если счёт упал и не появилось НОВОГО сообщения линтера."""
    errs0, n0, d0 = score(en, ru)
    cur, used = ru, []
    for name, rule in COSMETIC:
        if enabled and name not in enabled:
            continue
        cand = rule(en, cur)
        if cand and cand != cur and score(en, cand)[1:] <= (n0, d0):
            cur, _ = cand, used.append(name)
    if not errs0:
        return (cur, used) if cur != ru else None
    for _ in range(4):
        for name, rule in RULES:
            if enabled and name not in enabled:
                continue
            try:
                cand = rule(en, cur)
            except Exception:
                cand = None
            if not cand or cand == cur:
                continue
            errs, n, d = score(en, cand)
            _, ncur, dcur = score(en, cur)
            if (n, d) < (ncur, dcur) and not (errs - errs0):
                cur, _ = cand, used.append(name)
        if cur != ru and not score(en, cur)[0]:
            break
    return (cur, used) if cur != ru else None


def process(fn, enabled, apply, stats, examples):
    raw = open(fn, 'rb').read()
    lt = '\r\n' if b'\r\n' in raw else '\n'
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8'))))
    buf = io.StringIO(); csv.writer(buf, lineterminator=lt).writerows(rows)
    if buf.getvalue().encode('utf-8') != raw:
        print(f"  ПРОПУСК {os.path.basename(fn)}: не проходит round-trip (не трогаю)")
        return 0
    changed = 0
    for i, r in enumerate(rows, start=1):
        if len(r) < 2 or not r[1].strip() or r[0].lower() == 'english':
            continue
        got = fix_row(r[0], r[1], enabled)
        if not got:
            continue
        new, names = got
        key = '+'.join(dict.fromkeys(names))
        stats[key] += 1
        if len(examples[key]) < 3:
            examples[key].append((os.path.basename(fn), i, r[0][:90], r[1][:90], new[:90]))
        r[1] = new
        changed += 1
    if changed and apply:
        buf = io.StringIO(); csv.writer(buf, lineterminator=lt).writerows(rows)
        open(fn, 'wb').write(buf.getvalue().encode('utf-8'))
    return changed


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    apply = '--apply' in sys.argv
    show_ex = '--examples' in sys.argv
    enabled = set(args) or None
    known = {n for n, _ in RULES} | {n for n, _ in COSMETIC}
    if enabled and (enabled - known):
        print(f"Неизвестные правила: {', '.join(sorted(enabled - known))}")
        return 1

    files = sorted(glob.glob(os.path.join(GCDIR, 'dict_*.csv')))
    ms = os.path.join(GCDIR, 'main_strings.csv')
    if os.path.exists(ms):
        files.append(ms)

    stats, examples, total = collections.Counter(), collections.defaultdict(list), 0
    for fn in files:
        n = process(fn, enabled, apply, stats, examples)
        if n:
            print(f"  {os.path.basename(fn)}: {n}")
        total += n

    print(f"\n{'Исправлено' if apply else 'Будет исправлено'} строк: {total}")
    for name, cnt in stats.most_common():
        print(f"  {cnt:6}  {name}")
        if show_ex:
            for f, i, en, ru, new in examples[name]:
                print(f"          {f}:{i}\n            EN  {en!r}\n            было {ru!r}\n            стало {new!r}")
    if not apply and total:
        print("\nЭто пробный прогон. Записать: python fix_dicts.py --apply")
    return 0


if __name__ == '__main__':
    sys.exit(main())
