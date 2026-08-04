#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
dict_tool.py — работа напрямую с dictionary.bin (формат GCDCT2).

CSV-слой (dict_*.csv, main_strings.csv, pn_*.csv) удалён, и bin стал
единственным источником истины. Поэтому инструмент читает и пишет bin сам,
не полагаясь на csv_to_bin.py.

Что это меняет по сравнению с прежним CSV-пайплайном
----------------------------------------------------
  * `crowdsource/csv_to_bin.py` и `make_release.py --build` больше не соберут
    bin — им нечего читать. Сборка теперь здесь.
  * `crowdsource/merge_back.py` (батчи -> CSV) тоже остался без целей.
    Его заменяет команда `frombatches` (батчи -> bin напрямую).
  * `make_release.py --no-build` по-прежнему годится: он читает заголовок bin.
    Его линт игровых словарей молча ничего не найдёт — вместо него `broken`.
  * Если CSV когда-нибудь понадобятся снова — `export` соберёт их из bin.

Команды
-------
  Чтение:
    diff <чужой.bin>              что есть у них и нет у нас
    audit <чужой.bin>             гейты качества по чужому словарю
    verify <старый.bin>           потеряно/изменено + канон
    overwrites <старый.bin>       что стало с нашими переводами
    broken                        разбор битых строк по типу расхождения
    long <старый.bin>             проверка длинной прозы
    ratio                         длина русского против английского
    trunc <старый.bin>            обрывы, оставшиеся от чужого CSV-разбора

  Запись (все пишут прямо в dictionary.bin, с бэкапом рядом):
    merge <чужой.bin> --apply     влить чужой словарь
    frombatches --apply           влить переводы батчей (замена merge_back)
    canon --apply                 привести к канону терминов
    broken --fix-br --fix-tags    механические починки токенов
    trunc <старый.bin> --revert   откатить обрывы
    overwrites <старый.bin> --fix-regressions
    restore <старый.bin> --apply  вернуть пропавшие переводы
    export <папка>                выгрузить bin обратно в CSV

  Батчи:
    fillbatches --apply           закрыть строки батчей переводами из bin

Рядом лежит charscan.py — тот же bin, но про невидимые символы и пунктуацию
(потерянные переводы строк, плейсхолдеры, неразрывные пробелы, краевые пробелы).
Он только читает; чинят найденное команды отсюда.

ПОСЛЕ КАЖДОЙ ПИШУЩЕЙ КОМАНДЫ: прогнать гейт `charscan.py compare <бэкап>` и
обновить `crowdsource/DEFECTS.md` — реестр поломок с состоянием «убрано / убрано
частично / не тронуто». Порядок ведения описан в конце самого файла. Правка, не
дошедшая до реестра, через неделю будет найдена заново.
"""
import argparse, collections, csv, difflib, glob, io, os, re, shutil, struct, sys, time

csv.field_size_limit(1 << 30)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CROWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # crowdsource
ROOT = os.path.dirname(CROWD)                                        # glyphCore
OUR_BIN = os.path.join(ROOT, "dictionary.bin")
BAKDIR = os.path.join(CROWD, ".dict_bak")
sys.path.insert(0, CROWD)
try:
    import validate as _validate           # штатный линтер проекта
except Exception:
    _validate = None

FNV_OFFSET, FNV_PRIME, MASK64 = 0xcbf29ce484222325, 0x100000001b3, (1 << 64) - 1
MAGIC = b"GCDCT2"

TOK = re.compile(r'%\w+%|<[^>]+>|\[lbracket\]|\[rbracket\]|\[null\]')
PH = re.compile(r'%\w+%')
LEFTOVER = re.compile(r'\[s\]|\[pl:')
CYR = re.compile(r'[А-Яа-яЁё]')
# Служебные токены. [plur]/[nosep] — такие же токены движка, как [null]:
# validate.py знает их в KNOWN_TOKENS, и в переводе они остаются как есть.
SERVICE = re.compile(r'%\w+%|<[^>]+>|\[(?:lbracket|rbracket|null|plur|nosep|topic-[fm]|f|an|the)\]|\[pl:"[^"]*"\]|https?://\S+|www\.\S+|/\.\w+')
LAT = re.compile(r'[A-Za-z]{3,}')
# Латиница, которая у нас осознанна. Названия дополнений сюда НЕ входят:
# пользователь 2026-08-03 подтвердил, что дополнения переводятся.
SANCTIONED = re.compile(r"Guild Wars|ArenaNet|PvP|WvW|Mark [IVX]+|\bSAB\b|/\w+", re.I)
EN_FUNC = re.compile(r"\b(the|of|and|to|a|an|in|for|is|are|you|your|this|with|"
                     r"from|on|at|by|be|will|can|has|have)\b", re.I)
PLURAL_RU = re.compile(r'\[([^\]|]*)\|[^\]]*\]')
PLURAL_EN = re.compile(r'\[s\]|\[pl:"[^"]*"\]')
# След плейсхолдера, выпавшего из самого оригинала: двойной пробел,
# пробел перед знаком или заглушка «X%» вместо «%num1%%».
EN_SWALLOWED = re.compile(r"\w  \w|  \w|\s[.,]|(?<![A-Za-z])[Xx]%")


def fnv1a_u16(s):
    """FNV-1a-64 по код-юнитам UTF-16LE — тот же хеш, что в моде."""
    h = FNV_OFFSET
    for ch in s.encode("utf-16-le"):
        h ^= ch
        h = (h * FNV_PRIME) & MASK64
    return h


# ---------------------------------------------------------------- bin I/O
def read_sections(path):
    """GCDCT2 -> [(имя категории, [(hash, english, русский), ...]), ...].

    Имя категории отдаём КАК ЕСТЬ, вместе с «\\x1f<отображаемое имя>»:
    при обратной записи его нельзя терять, иначе в оверлее пропадут
    русские названия категорий.
    """
    with open(path, "rb") as f:
        b = f.read()
    if b[:6] != MAGIC:
        sys.exit("не словарь GlyphCore: сигнатура %r" % b[:6])
    p = 8
    struct.unpack_from("<I", b, p); p += 4
    ncat, = struct.unpack_from("<H", b, p); p += 2
    heads = []
    for _ in range(ncat):
        ln = b[p]; p += 1
        name = b[p:p + ln].decode("utf-8", "replace"); p += ln
        cnt, = struct.unpack_from("<I", b, p); p += 4
        off, = struct.unpack_from("<Q", b, p); p += 8
        heads.append((name, cnt, off))
    out = []
    for name, cnt, off in heads:
        q, es = off, []
        for _ in range(cnt):
            q += 4                                    # id записи, пересчитывается
            h, = struct.unpack_from("<Q", b, q); q += 8
            l, = struct.unpack_from("<H", b, q); q += 2
            en = b[q:q + l * 2].decode("utf-16-le", "replace"); q += l * 2
            l, = struct.unpack_from("<H", b, q); q += 2
            ru = b[q:q + l * 2].decode("utf-16-le", "replace"); q += l * 2
            es.append((h, en, ru))
        out.append((name, es))
    return out


def write_bin(path, sections):
    """Записать GCDCT2. Раскладка байт-в-байт как у csv_to_bin.build().

    Внутри категории записи сортируются по хешу и дедуплицируются
    (побеждает первая) — иначе оверлей может выбрать не ту.
    Пишем во временный файл и подменяем: если игра держит bin открытым,
    падение произойдёт до порчи рабочего файла.
    """
    norm = []
    for name, es in sections:
        seen, ded = set(), []
        for h, en, ru in sorted(es, key=lambda e: e[0]):
            if h in seen:
                continue
            seen.add(h)
            ded.append((h, en, ru))
        if ded:
            norm.append((name, ded))
    total = sum(len(es) for _, es in norm)
    header = 8 + 4 + 2
    dir_size = sum(1 + len(n.encode("utf-8")) + 4 + 8 for n, _ in norm)
    offset = header + dir_size
    offsets = []
    for _, es in norm:
        offsets.append(offset)
        for h, en, ru in es:
            offset += 4 + 8 + 2 + len(en.encode("utf-16-le")) + 2 + len(ru.encode("utf-16-le"))
    out = bytearray()
    out += MAGIC + b"\x00\x00"
    out += struct.pack("<I", total)
    out += struct.pack("<H", len(norm))
    for i, (name, es) in enumerate(norm):
        nb = name.encode("utf-8")
        out += struct.pack("<B", len(nb)); out += nb
        out += struct.pack("<I", len(es)); out += struct.pack("<Q", offsets[i])
    gid = 0
    for _, es in norm:
        for h, en, ru in es:
            enu = en.encode("utf-16-le"); tru = ru.encode("utf-16-le")
            out += struct.pack("<I", gid); out += struct.pack("<Q", h)
            out += struct.pack("<H", len(enu) // 2); out += enu
            out += struct.pack("<H", len(tru) // 2); out += tru
            gid += 1
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, path)
    return len(norm), total


def backup(path):
    os.makedirs(BAKDIR, exist_ok=True)
    dst = os.path.join(BAKDIR, "%s.%s.bak" % (os.path.basename(path),
                                              time.strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(path, dst)
    return dst


def load_map(path):
    """{hash: (english, русский, категория без отображаемого имени)}."""
    out = {}
    for name, es in read_sections(path):
        cat = name.split("\x1f")[0]
        for h, en, ru in es:
            out.setdefault(h, (en, ru, cat))
    return out


def save_map(sections, changes, added):
    """Собрать секции обратно с учётом правок.

    changes: {hash: новый русский} — меняем на месте, порядок сохраняем.
    added:   {hash: (english, русский, категория)} — дописываем в свою категорию.
    """
    by_cat = collections.OrderedDict()
    for name, es in sections:
        by_cat[name] = [(h, en, changes.get(h, ru)) for h, en, ru in es]
    if added:
        short = {n.split("\x1f")[0]: n for n in by_cat}
        for h, (en, ru, cat) in added.items():
            key = short.get(cat) or short.get("основной") or next(iter(by_cat))
            by_cat[key].append((h, en, ru))
    return list(by_cat.items())


# ---------------------------------------------------------------- гейты
_NAMES = None
QUOTED = re.compile(r'«[^»]*»|"[^"]*"|„[^“]*“')
CODEWORD = re.compile(r'^[A-Z0-9][A-Z0-9\.\-\']*$')       # BUY-4373, FTT, K1T-D


def name_layer():
    """Латинские слова из категорий pn_* словаря.

    В этом проекте имена собственные можно оставлять латиницей: их
    подхватывает слой pn_*, и движок транслитерирует их сам. Поэтому такая
    латиница — не брак, и считать её дефектом нельзя (иначе гейт отклоняет
    корректные ручные переводы тысячами).
    """
    global _NAMES
    if _NAMES is None:
        _NAMES = set()
        try:
            for name, es in read_sections(OUR_BIN):
                if not name.split("\x1f")[0].startswith("pn_"):
                    continue
                for _h, en, _ru in es:
                    for w in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", en):
                        _NAMES.add(w.lower())
        except SystemExit:
            pass
    return _NAMES


def stray_latin(ru, en=""):
    """Латиница, которую нельзя объяснить конвенциями проекта.

    Не считаем браком:
      * имена из слоя pn_* — движок их транслитерирует;
      * названия в кавычках («Astralaria», "Kraitkin");
      * коды и идентификаторы (BUY-4373, K1T-D);
      * слова С ЗАГЛАВНОЙ, которые есть и в оригинале, — это имена
        собственные и названия, оставленные намеренно: события
        (Dragon Bash, Extra Life) и компоненты рецептов (Gift of Mastery).
    Остаётся то, что и должно ловиться: забытые строчные слова, то есть
    непереведённая проза.
    """
    rest = SANCTIONED.sub("", SERVICE.sub("", ru))
    rest = QUOTED.sub(" ", rest)
    names = name_layer()
    en_words = set(re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", en))
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", rest):
        if w.lower() in names or CODEWORD.match(w):
            continue
        if w[:1].isupper() and w in en_words:
            continue
        out.append(w)
    return out


def defects(en, ru):
    """Те же проверки, что блокируют сдачу батча, плюс латиница/непереведённое."""
    d = []
    if sorted(TOK.findall(en)) != sorted(TOK.findall(ru)):
        d.append("токены")
    if PH.sub("", en).count("%") != PH.sub("", ru).count("%"):
        d.append("%")
    if LEFTOVER.search(ru):
        d.append("[pl:/[s]")
    if "�" in ru:
        d.append("U+FFFD")
    if not ru.strip():
        d.append("пусто")
    elif ru.strip() == en.strip():
        # «Secrets of the Obscure» латиницей — канон, а не забытый перевод;
        # дефект только если это фраза, а не название.
        if EN_FUNC.search(en) and len(en.split()) > 3:
            d.append("не переведено")
    elif not CYR.search(ru):
        d.append("без кириллицы")
    elif stray_latin(ru, en):
        d.append("латиница")
    return d


# ---------------------------------------------------------------- канон
SKIN = {"скин": "облик", "скина": "облика", "скину": "облику", "скином": "обликом",
        "скине": "облике", "скины": "облики", "скинов": "обликов",
        "скинам": "обликам", "скинами": "обликами", "скинах": "обликах"}
RX_SKIN = re.compile(r"(?<![А-Яа-яЁё])([Сс])(кин(?:ов|ами|ам|ах|ом|а|у|е|ы)?)(?![А-Яа-яЁё])")
LAT2CYR = {"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
           "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
           "O": "О", "P": "Р", "T": "Т", "X": "Х"}
WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
EXPANSIONS = (("Секреты Недр", "Тайны Сокрытого"),
              ("Тайны Запределья", "Тайны Сокрытого"),
              ("Сердце Шипов", "Сердце тернового леса"),
              ("Дикие земли Джантира", "Дебри Джантира"),
              ("Видения Вечности", "Видения вечности"),
              ("Сага о Ледоклыке", "Сага о Ледоклыках"),
              ("Ледяная Сага", "Сага о Ледоклыках"),
              ("Коробка супер-приключений", "Супер Приключенческая Коробка"))


def _skin(m):
    w = m.group(1) + m.group(2)
    r = SKIN.get(w.lower())
    if not r:
        return w
    return r.capitalize() if w[0].isupper() else r


CYR2LAT = {v: k for k, v in LAT2CYR.items()}


def fix_homoglyphs(s):
    """Латинские буквы внутри русского слова — след машинного перевода.

    Правим ТОЛЬКО если слово преимущественно кириллическое. В игровых строках
    латиница и кириллица часто идут слитно без разделителя
    («RuinbringerИмператорBlood»), и без этой проверки правило калечило
    английскую часть, подменяя в ней буквы на кириллические.
    """
    def rep(m):
        w = m.group(0)
        cyr = sum(1 for c in w if not c.isascii())
        lat = sum(1 for c in w if c.isascii() and c.isalpha())
        if not cyr or lat >= cyr:
            return w
        return "".join(LAT2CYR.get(c, c) if c.isascii() and c.isalpha() else c
                       for c in w)
    return WORD.sub(rep, s)


def unfix_homoglyphs(s):
    """Обратная правка: вернуть латиницу там, где прежняя версия
    fix_homoglyphs подменила её кириллическим двойником.

    Смотрим на соседей буквы, а не на слово целиком: в склейках вида
    «RuinbringerИмператорBlood» кириллица и латиница живут в одном «слове»,
    и по словарной статистике их не разделить, а по соседям — можно.
    Буква-двойник (о, а, е, с, р, х, В, Т…) превращается обратно в латиницу,
    только если рядом стоит однозначно латинская буква и нет однозначно
    кириллической.
    """
    def certain(c):
        """'lat' / 'cyr' / None для неоднозначных двойников."""
        if not c.isalpha():
            return None
        if c.isascii():
            return None if c in LAT2CYR else "lat"
        return None if c in CYR2LAT else "cyr"

    def rep(m):
        w = list(m.group(0))
        out, changed = list(w), False
        for i, c in enumerate(w):
            if c not in CYR2LAT:
                continue
            # Требуем однозначную латиницу с ОБЕИХ сторон. По одному
            # соседу рискованно: в тексте попадается литерал вида
            # backslash-r-n, и тогда «n» стоит вплотную к русскому
            # «Содержит» — правило по одному соседу переделывало «С»
            # в латинскую «C». Плата за строгость: буква на самой
            # границе склейки («В» в «…ИмператорВlood») остаётся
            # неисправленной. Это безопаснее, чем портить русские слова.
            left = next((certain(w[j]) for j in range(i - 1, -1, -1)
                         if certain(w[j])), None)
            right = next((certain(w[j]) for j in range(i + 1, len(w))
                          if certain(w[j])), None)
            if left == "lat" and right == "lat":
                out[i] = CYR2LAT[c]
                changed = True
        return "".join(out) if changed else m.group(0)
    return WORD.sub(rep, s)


def normalize(en, ru):
    """Канон проекта. Меняем только кириллицу, поэтому токены задеть нельзя."""
    out = RX_SKIN.sub(_skin, ru)
    out = re.sub(r"(?<![А-Яа-яЁё])([Сс])кайскал", lambda m: m.group(1) + "кайскейл", out)
    out = re.sub(r"(?<![А-Яа-яЁё])([Дд])оляк", lambda m: m.group(1) + "ольяк", out)
    out = out.replace("Жайтан", "Зайтан")          # решение пользователя 2026-08-03
    for a, b in EXPANSIONS:
        out = out.replace(a, b)
    # ascended -> вознесённый, но Exalted -> возвышенный оставляем как есть
    if "ascend" in en.lower():
        out = re.sub(r"(?<![А-Яа-яЁё])([Вв])озвышенн",
                     lambda m: m.group(1) + "ознесённ", out)
    return fix_homoglyphs(out)


# ---------------------------------------------------------------- обрывы
END_OK = ('.', '!', '?', '…', ')', '»', '"', ':', '*', ']', '—', '–')


def looks_truncated(en, ru):
    """Текст обрезан при разборе CSV на чужой стороне: потеряно всё после
    первой запятой оригинала. Короткие названия под правило не попадают."""
    e, r = SERVICE.sub("", en).strip(), SERVICE.sub("", ru).strip()
    if len(e) < 60 or not r or r.endswith(END_OK):
        return False
    ratio = len(r) / len(e)
    if "," in e and "," not in r and ratio < 0.85:
        return True
    return e.endswith((".", "!", "?", "…")) and ratio < 0.7


def _body(s):
    """Без служебных токенов и без раздувания плюрал-группами."""
    s = SERVICE.sub("", s)
    s = PLURAL_RU.sub(r"\1", s)
    s = PLURAL_EN.sub("", s)
    return s.strip()


def batch_files():
    for fp in sorted(glob.glob(os.path.join(CROWD, "*", "*.csv"))):
        p = fp.replace("\\", "/")
        if ".batch_bak" in p or "/sync/" in p:
            continue
        yield fp


def human_pairs():
    """{english: русский} из батчей — наш ручной перевод."""
    out = {}
    for fp in batch_files():
        try:
            with open(fp, encoding="utf-8-sig", newline="") as f:
                for r in csv.reader(f):
                    if len(r) >= 2 and r[0].strip() and r[1].strip() \
                            and r[0].strip().lower() != "english":
                        out.setdefault(r[0], r[1])
        except Exception:
            pass
    return out


def apply_changes(changes, added, what):
    """Записать правки в bin с бэкапом."""
    if not changes and not added:
        print("нечего менять")
        return
    sections = read_sections(OUR_BIN)
    bak = backup(OUR_BIN)
    ncat, total = write_bin(OUR_BIN, save_map(sections, changes, added))
    print("%s: изменено %d, добавлено %d | в bin %d категорий, %d записей"
          % (what, len(changes), len(added), ncat, total))
    print("бэкап: %s" % os.path.relpath(bak, ROOT))


# ---------------------------------------------------------------- команды
def cmd_diff(a):
    ours, theirs = load_map(a.our), load_map(a.foreign)
    only = [h for h in theirs if h not in ours]
    both = [h for h in theirs if h in ours]
    diff = [h for h in both if theirs[h][1].strip() != ours[h][1].strip()]
    print("наш:   %d записей\nчужой: %d записей" % (len(ours), len(theirs)))
    print("только у них: %d | общих: %d (перевод расходится: %d) | только у нас: %d"
          % (len(only), len(both), len(diff), len([h for h in ours if h not in theirs])))
    for cat, n in collections.Counter(theirs[h][2] for h in only).most_common(15):
        print("  %-28s %6d" % (cat, n))


def cmd_audit(a):
    ours, theirs = load_map(a.our), load_map(a.foreign)
    new = [(h, *theirs[h]) for h in theirs if h not in ours]
    kinds = collections.Counter()
    for _h, en, ru, _c in new:
        for d in defects(en, ru):
            kinds[d] += 1
    print("кандидатов (нет у нас): %d" % len(new))
    for k, n in kinds.most_common():
        print("  %-18s %6d  (%.1f%%)" % (k, n, 100.0 * n / max(1, len(new))))


def cmd_merge(a):
    ours, theirs = load_map(a.our), load_map(a.foreign)
    human = {fnv1a_u16(en) for en in human_pairs()}
    changes, added = {}, {}
    skipped = collections.Counter()
    for h, (en, ru, cat) in theirs.items():
        mine = ours.get(h)
        if mine is None:
            if defects(en, ru):
                skipped["дефект у них"] += 1
                continue
            ru2 = normalize(en, ru)
            if defects(en, ru2):
                skipped["сломала нормализация"] += 1
                continue
            added[h] = (en, ru2, cat)
        else:
            if mine[1].strip() == ru.strip():
                continue
            if h in human:
                skipped["ручной перевод"] += 1
                continue
            if defects(mine[0], mine[1]) and not defects(en, ru):
                ru2 = normalize(en, ru)
                if defects(en, ru2):
                    skipped["сломала нормализация"] += 1
                    continue
                changes[h] = ru2
    print("добавить: %d | заменить наши битые: %d" % (len(added), len(changes)))
    for k, n in skipped.most_common():
        print("  пропущено (%s): %d" % (k, n))
    if a.apply:
        apply_changes(changes, added, "вливание")
    else:
        print("(план; для записи --apply)")


def cmd_frombatches(a):
    """Батчи -> bin. Замена merge_back.py, который работал через CSV.

    Заполняем то, чего в словаре нет, и заменяем наши строки там, где у нас
    дефект, а батчевый перевод чист: батч прошёл линтер и вычитан человеком.
    """
    ours = load_map(OUR_BIN)
    changes, added = {}, {}
    rejected = 0
    rej_rows, rej_kinds = [], collections.Counter()
    for en, ru in human_pairs().items():
        h = fnv1a_u16(en)
        # Гейт строгий (defects), а не мягкий линтер батчей: в словарь
        # не должно попадать ничего с латиницей, битыми токенами или
        # остатками [pl:. Отклонённые строки видно в отчёте — их правят
        # в батче и вливают следующим прогоном.
        d = defects(en, ru)
        if d:
            rejected += 1
            rej_kinds[",".join(d)] += 1
            rej_rows.append((",".join(d), en, ru))
            continue
        cur = ours.get(h)
        if cur is None:
            added[h] = (en, ru, "основной")
        elif cur[1].strip() != ru.strip() and defects(cur[0], cur[1]):
            changes[h] = ru
    print("из батчей: добавить %d | заменить наши битые %d | отклонено гейтами %d"
          % (len(added), len(changes), rejected))
    for k, n in rej_kinds.most_common(8):
        print("    отклонено — %-22s %5d" % (k, n))
    if rej_rows:
        out = os.path.join(CROWD, "sync", "reports", "batch_rejected.csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["причина", "english", "перевод в батче"])
            w.writerows(rej_rows)
        print("    отчёт по отклонённым: %s" % os.path.relpath(out, ROOT))
    if a.apply:
        apply_changes(changes, added, "вливание батчей")
    else:
        print("(план; для записи --apply)")


def cmd_canon(a):
    ours = load_map(OUR_BIN)
    changes = {}
    for h, (en, ru, _c) in ours.items():
        v = normalize(en, ru)
        if v != ru:
            changes[h] = v
    print("строк под канон: %d" % len(changes))
    if a.apply:
        apply_changes(changes, {}, "канон")
    else:
        print("(для записи --apply)")


def cmd_broken(a):
    ours = load_map(OUR_BIN)
    kinds = collections.Counter()
    rows = []
    for h, (en, ru, cat) in ours.items():
        te, tr = collections.Counter(TOK.findall(en)), collections.Counter(TOK.findall(ru))
        if te == tr:
            continue
        miss, extra = te - tr, tr - te
        if {k.lower() for k in miss.elements()} == {k.lower() for k in extra.elements()} \
                and miss and extra:
            kind = "регистр тега различается"
        elif miss and not extra:
            kind = "потеряно: " + ", ".join(sorted({m for m in miss.elements()})[:2])
        elif extra and not miss:
            kind = "лишнее: " + ", ".join(sorted({m for m in extra.elements()})[:2])
        else:
            kind = "подменено: %s -> %s" % (
                ",".join(sorted({m for m in miss.elements()})[:2]),
                ",".join(sorted({m for m in extra.elements()})[:2]))
        kinds[kind] += 1
        rows.append((kind, cat, h, en, ru))
    print("записей в словаре: %d | битых: %d" % (len(ours), len(rows)))
    for k, n in kinds.most_common(20):
        print("  %-52s %5d" % (k[:52], n))
    out = os.path.join(CROWD, "sync", "reports", "dict_broken.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["тип", "категория", "english", "русский"])
        for kind, cat, _h, en, ru in rows:
            w.writerow([kind, cat, en, ru])
    print("отчёт: %s" % os.path.relpath(out, ROOT))
    if not (a.fix_br or a.fix_tags):
        print("починка: --fix-br, --fix-tags")
        return
    lead = re.compile(r'^(<c=@\w+>)\s*([^<]{1,40}?)\s*(</c>)')
    changes = {}
    for kind, cat, h, en, ru in rows:
        new = ru
        if a.fix_br:
            need = en.count("<br>") - ru.count("<br>")
            if need > 0 and new.count("\n") == need:
                new = new.replace("\n", "<br>")
        if a.fix_tags:
            m = lead.match(en)
            if m and m.group(1) not in new:
                m2 = re.match(r'^\s*([^<\n]{1,40}?[.:])\s*', new)
                if m2:
                    new = "%s%s%s%s" % (m.group(1), m2.group(1), m.group(3), new[m2.end():])
        if new != ru and sorted(TOK.findall(en)) == sorted(TOK.findall(new)):
            changes[h] = new
    print("починено механически: %d" % len(changes))
    apply_changes(changes, {}, "починка токенов")


QUOTED_SQL = re.compile(r"^'(.*)'$", re.S)
ESCAPES = ((r"\r\n", "\n"), (r"\n", "\n"), (r"\r", "\n"),
           (r"\t", "\t"), (r"\"", '"'), (r"\\", "\\"))


def sql_unquote(s):
    """Снять обёртку из SQL-выгрузки: 'текст с ''апострофом''' -> текст с 'апострофом'."""
    m = QUOTED_SQL.match(s)
    if not m:
        return None
    return m.group(1).replace("''", "'")


def unescape(s):
    """Развернуть литеральные escape: два символа «\\» и «n» -> перевод строки."""
    for a, b in ESCAPES:
        s = s.replace(a, b)
    return s


def batch_english():
    """Множество английских строк из батчей.

    Батчи выгружены прямо из игры, поэтому служат оракулом: если наша
    испорченная строка после распрямления находится здесь — реконструкция
    доказана, а не угадана.
    """
    out = set()
    for fp in batch_files():
        try:
            with open(fp, encoding="utf-8-sig", newline="") as f:
                for r in csv.reader(f):
                    if r and r[0].strip() and r[0].strip().lower() != "english":
                        out.add(r[0])
        except Exception:
            pass
    return out


def cmd_unquote(a):
    """Воскресить записи, у которых английский обёрнут в SQL-кавычки.

    Такая запись мертва: хеш посчитан от испорченной строки, и в игре она
    не совпадёт никогда — игра хеширует настоящий текст. Чинить надо не
    только текст, но и хеш, поэтому обычный apply_changes тут не годится.

    Распрямляем только то, что подтверждает оракул (батчи из игры). Если у
    восстановленного хеша уже есть живая запись — мёртвую выбрасываем,
    иначе перезаписываем хеш, английский и перевод.
    """
    sections = read_sections(OUR_BIN)
    oracle = batch_english()
    live = set()
    for _n, es in sections:
        for h, en, _ru in es:
            if en and fnv1a_u16(en) == h:
                live.add(h)

    out, revived, dropped, skipped = [], 0, 0, 0
    ex_rev, ex_drop = [], []
    for name, es in sections:
        keep = []
        for h, en, ru in es:
            v = en and sql_unquote(en)
            if not v or v == en or v not in oracle:
                if en and sql_unquote(en) not in (None, en):
                    skipped += 1
                keep.append((h, en, ru))
                continue
            nh = fnv1a_u16(v)
            if nh in live:
                dropped += 1
                if len(ex_drop) < 3:
                    ex_drop.append((name, en, ru))
                continue
            nru = sql_unquote(ru)
            nru = nru if nru is not None else ru
            keep.append((nh, v, nru))
            live.add(nh)
            revived += 1
            if len(ex_rev) < 5:
                ex_rev.append((name, en, v, ru, nru))
        out.append((name, keep))

    print("мёртвых записей (английский в кавычках): %d" % (revived + dropped + skipped))
    print("  воскрешено (хеш пересчитан):        %d" % revived)
    print("  выброшено как мёртвый дубликат:     %d" % dropped)
    print("  оставлено (оракул не подтвердил):   %d" % skipped)
    for name, en, v, ru, nru in ex_rev:
        print("\n  [%s]\n  EN было  %r\n  EN стало %r" % (name.split("\x1f")[0], en[:90], v[:90]))
        if nru != ru:
            print("  RU было  %r\n  RU стало %r" % (ru[:90], nru[:90]))
    if not a.apply:
        print("\n(для записи --apply)")
        return
    bak = backup(OUR_BIN)
    ncat, total = write_bin(OUR_BIN, out)
    print("\nзаписано: %d категорий, %d записей | бэкап: %s"
          % (ncat, total, os.path.relpath(bak, ROOT)))


def batch_key_index():
    """Английские строки батчей по ключу «без плейсхолдеров и лишних пробелов».

    Батчи выгружены из игры, поэтому их английский — эталон. Ключ гасит ровно ту
    порчу, которую надо опознать: выпавший «%num1%» и оставшийся на его месте
    двойной пробел.
    """
    idx = collections.defaultdict(set)
    for en in batch_english():
        idx[en_key(en)].add(en)
    return idx


def en_key(s):
    s = PH.sub(" ", s)
    s = re.sub(r"\b[Xx]%", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def cmd_enbroken(a):
    """Записи, у которых плейсхолдер выпал из САМОГО английского.

    Хеш посчитан от огрызка, поэтому в игре запись не всплывёт никогда: игра
    хеширует настоящую строку. Настоящую берём из батчей — там английский прямо
    из игры. Дальше два исхода: если хеш настоящей строки свободен, запись можно
    оживить (переписать хеш и английский); если занят, значит живой близнец уже
    есть, и огрызок — мусор, который только путает счётчики и поиск.

    Выбрасываем только тогда, когда у живого близнеца перевод не хуже: все
    плейсхолдеры оригинала на месте. Иначе запись остаётся и попадает в отчёт.
    """
    sections = read_sections(OUR_BIN)
    ours = load_map(OUR_BIN)
    idx = batch_key_index()
    revive, drop, keep = {}, set(), 0
    ex_rev, ex_drop = [], []
    for h, (en, ru, _c) in ours.items():
        if not en or not ru:
            continue
        extra = collections.Counter(PH.findall(ru)) - collections.Counter(PH.findall(en))
        if not extra or (collections.Counter(PH.findall(en)) - collections.Counter(PH.findall(ru))):
            continue
        if not EN_SWALLOWED.search(en):
            continue
        cand = idx.get(en_key(en))
        if not cand or len(cand) > 1:
            keep += 1
            continue
        real = next(iter(cand))
        if real == en:
            keep += 1
            continue
        nh = fnv1a_u16(real)
        if nh not in ours:
            revive[h] = (nh, real)
            if len(ex_rev) < 3:
                ex_rev.append((en, real, ru))
        elif sorted(PH.findall(ours[nh][1])) == sorted(PH.findall(real)):
            drop.add(h)
            if len(ex_drop) < 3:
                ex_drop.append((en, real, ru, ours[nh][1]))
        else:
            keep += 1
    print("огрызков английского: %d" % (len(revive) + len(drop) + keep))
    print("  оживить (хеш свободен):                  %d" % len(revive))
    print("  выбросить как мёртвый дубликат:          %d" % len(drop))
    print("  оставить (батчи не подтвердили/перевод живого хуже): %d" % keep)
    for en, real, ru in ex_rev:
        print("\n  было  %r\n  стало %r\n  RU    %r" % (en[:85], real[:85], ru[:60]))
    for en, real, ru, lru in ex_drop:
        print("\n  огрызок %r\n  настоящая %r\n  перевод огрызка %r\n  перевод живой  %r"
              % (en[:80], real[:80], ru[:60], lru[:60]))
    if not a.apply:
        if revive or drop:
            print("\n(для записи --apply)")
        return
    out = []
    for name, es in sections:
        keep_es = []
        for h, en, ru in es:
            if h in drop:
                continue
            if h in revive:
                nh, real = revive[h]
                keep_es.append((nh, real, ru))
                continue
            keep_es.append((h, en, ru))
        out.append((name, keep_es))
    bak = backup(OUR_BIN)
    ncat, total = write_bin(OUR_BIN, out)
    print("\nоживлено %d, выброшено %d | в bin %d категорий, %d записей"
          % (len(revive), len(drop), ncat, total))
    print("бэкап: %s" % os.path.relpath(bak, ROOT))


def cmd_escapes(a):
    """Развернуть литеральные escape в переводах.

    В переводе стоят два символа «\\» и «n» вместо перевода строки — игра их
    не разворачивает и рисует как мусор прямо в диалогах. Меняем только там,
    где число разворачиваемых переносов не превышает числа настоящих
    переносов в оригинале: иначе это не escape, а часть текста.
    """
    ours = load_map(OUR_BIN)
    changes, refused = {}, 0
    for h, (en, ru, _c) in ours.items():
        if "\\" not in ru:
            continue
        v = unescape(ru)
        if v == ru:
            continue
        if en and v.count("\n") > en.count("\n"):
            refused += 1
            continue
        changes[h] = v
    print("переводов с литеральными escape: %d (отклонено: %d)" % (len(changes), refused))
    for h in list(changes)[:5]:
        print("  было:  %r" % ours[h][1][:90])
        print("  стало: %r" % changes[h][:90])
    if a.apply:
        apply_changes(changes, {}, "разворот escape")
    elif changes:
        print("(для записи --apply)")


def cmd_extratok(a):
    """Убрать из перевода токены, которых нет в оригинале.

    Бывает двух видов. Группа склонения «[Шлем|Шлема|Шлемов] убийцы» там, где
    в оригинале нет числа: склонять не от чего, игра покажет скобки как есть —
    схлопываем в первую форму. И плейсхолдер «%num1%», которого в оригинале
    тоже нет, — его просто выбрасываем.

    Трогаем только записи, чей английский подтверждён батчами: батч выгружен
    из игры, значит строка настоящая и дефект в переводе, а не в оригинале.
    Иначе запись всё равно мертва, и править её нет смысла.
    """
    ours = load_map(OUR_BIN)
    oracle = batch_english()
    plur = re.compile(r"\[([^\]\[|]*)\|[^\]\[]*\]")
    changes, kinds, skipped = {}, collections.Counter(), 0
    for h, (en, ru, _c) in ours.items():
        if not en or not ru:
            continue
        te, tr = collections.Counter(TOK.findall(en)), collections.Counter(TOK.findall(ru))
        extra = tr - te
        if not extra or (te - tr):
            continue
        if en not in oracle:
            skipped += 1
            continue
        new = ru
        for t in extra.elements():
            if plur.fullmatch(t):
                new = new.replace(t, plur.match(t).group(1), 1)
                kinds["схлопнута группа склонения"] += 1
            else:
                new = new.replace(t, "", 1)
                kinds["убран лишний плейсхолдер"] += 1
        new = re.sub(r" {2,}", " ", new).strip()
        if new and new != ru and not (collections.Counter(TOK.findall(new)) - te):
            changes[h] = new
    print("починено: %d | пропущено (оригинал не подтверждён батчем): %d"
          % (len(changes), skipped))
    for k, n in kinds.most_common():
        print("  %5d  %s" % (n, k))
    for h in list(changes)[:6]:
        print("\n  EN    %r\n  было  %r\n  стало %r"
              % (ours[h][0][:90], ours[h][1][:90], changes[h][:90]))
    if a.apply:
        apply_changes(changes, {}, "лишние токены")
    elif changes:
        print("\n(для записи --apply)")


# ------------------------------------------------- возврат потерянных переносов
# Перевод строки съеден ещё в исходном машинном переводе: «Чешуя...Когти...» вместо
# «Чешуя...\nКогти...». Место шва ищем свободно, а принимаем строго — по оригиналу:
# генератор может ошибаться, приёмка ошибаться не должна.
NL_HEAD = ("ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
           "0123456789+-−•*<([«\"'#")
NL_NUM = re.compile(r"\d+")
# Тире здесь тоже маркер: «—Warmaster Jofast» — это подпись, и она начинает
# строку. Без него шов встаёт после тире и оставляет его висеть в конце.
NL_LEAD = re.compile(r"^\s*([+\-−—–]|\*{1,2}|[•*]|#{1,3})")
NL_WS = re.compile(r"\s+")


def nl_breaks(en):
    """[(доля длины текста без переносов, пачка переносов, чем начинается строка после)].

    Подряд идущие \\n («\\n\\n» — абзац) считаем одним разрывом: в переводе им
    соответствует одно место, а не два. Начало следующей строки нужно, чтобы
    притянуть шов к маркеру («—подпись», «• пункт»).
    """
    out, seen, run = [], 0, ""
    body = len(en.replace("\n", "")) or 1
    for k, ch in enumerate(en):
        if ch == "\n":
            run += ch
        else:
            if run:
                out.append((seen / body, run, en[k:k + 8]))
                run = ""
            seen += 1
    if run:
        out.append((seen / body, run, ""))
    return out


def nl_candidates(ru):
    """Куда вставим: жёсткие швы (склейка без пробела) и мягкие (обычный пробел)."""
    hard, soft, n = [], [], len(ru) or 1
    for i in range(1, len(ru)):
        prev, cur = ru[i - 1], ru[i]
        if cur not in NL_HEAD:
            continue
        if not prev.isspace():
            hard.append((i / n, i))       # здесь был бы пробел — значит съеден перенос
        elif prev == " ":
            soft.append((i / n, i))       # перенос выродился в пробел
    return hard, soft


def nl_align(en, ru, tol_hard=0.18, tol_soft=0.07):
    """Сопоставить разрывы оригинала швам перевода -> ([(позиция, пачка)], качество)."""
    br = nl_breaks(en)
    if not br:
        return None, None
    hard, soft = nl_candidates(ru)
    picks, quality, cur = [], "жёстко", -1
    for rel, run, head in br:
        best = None
        for pool, tol, kind in ((hard, tol_hard, "h"), (soft, tol_soft, "s")):
            near = sorted((abs(r - rel), i, kind) for r, i in pool
                          if i > cur and abs(r - rel) <= tol)
            if not near:
                continue
            # два кандидата почти вплотную — место шва не определено, не гадаем
            if len(near) > 1 and near[1][0] < near[0][0] * 2 and near[1][0] < 0.05:
                return None, None
            best = near[0]
            break
        if best is None:
            return None, None
        if best[2] == "s":
            quality = "мягко"
        picks.append((nl_snap(ru, best[1], head), run))
        cur = picks[-1][0]
    return picks, quality


def nl_snap(ru, i, head):
    """Притянуть шов к маркеру начала строки.

    Оригинал говорит, чем начинается следующая строка («—Warmaster Jofast»,
    «• 5 плодов»). Если тот же маркер в переводе стоит вплотную слева от
    выбранного места, шов встал на символ правее и оставил маркер висеть
    в конце предыдущей строки — сдвигаем влево.
    """
    m = NL_LEAD.match(head or "")
    if not m:
        return i
    mark = m.group(1)
    for back in (1, 2):
        if i - back >= 0 and ru[i - back:i].strip() == mark:
            return i - back
    return i


def nl_apply(ru, picks):
    out, prev = [], 0
    for i, run in picks:
        out.append(ru[prev:i].rstrip(" "))
        out.append(run)
        prev = i
    out.append(ru[prev:])
    return "".join(out)


def nl_invariants(seg):
    """То, что перевод не меняет: числа, плейсхолдеры, теги, маркер строки, «:» в конце."""
    m = NL_LEAD.match(seg)
    return (sorted(NL_NUM.findall(seg)), sorted(PH.findall(seg)),
            sorted(re.findall(r"<[^>]+>", seg)), m.group(1) if m else "",
            seg.rstrip()[-1:] if seg.rstrip()[-1:] in ":." else "")


def nl_verify(en, ru_new):
    """Сегменты перевода сходятся с сегментами оригинала? -> (ок, сколько сверок).

    Сверка симметричная: расхождение считается и тогда, когда лишнее появилось
    в переводе, иначе шов, разорвавший «**» пополам, пройдёт незамеченным.
    """
    es, rs = en.split("\n"), ru_new.split("\n")
    if len(es) != len(rs):
        return False, 0
    # Длины сегментов должны тянуться за оригиналом: русский сегмент бывает в
    # полтора раза длиннее английского, но не в пять раз короче. Если в оригинале
    # строка длинная, а в переводе на её месте «</c>» — перевод потерял её целиком.
    te, tr = sum(len(s) for s in es) or 1, sum(len(s) for s in rs) or 1
    scale = tr / te
    for e, r in zip(es, rs):
        if len(e) < 6:                    # огрызок вроде «</c>» не о чем сравнивать
            continue
        k = (len(r) or 0.5) / len(e) / scale
        if k < 0.45 or k > 2.2:
            return False, 0
    checked = 0
    for e, r in zip(es, rs):
        ne, pe, te, le, ce = nl_invariants(e)
        nr, pr, tr, lr, cr = nl_invariants(r)
        for a, b, need_tags in ((ne, nr, False), (pe, pr, False), (te, tr, True)):
            if need_tags and not re.search(r"<[^>]+>", ru_new):
                continue                  # теги в переводе не сохраняли вовсе — не спрос
            if a or b:
                checked += 1
                if a != b:
                    return False, checked
        # Маркер строки: потерять нельзя, а вот появиться в переводе тире может —
        # русская реплика начинается с тире там, где в оригинале кавычки.
        if le or lr:
            checked += 1
            if le != lr and not (not le and lr in "—–"):
                return False, checked
        if ce or cr:
            checked += 1
            if (ce == ":") != (cr == ":"):
                return False, checked
    return True, checked


# ---- оракул: чужой перевод той же строки, у которого переносы уцелели --------
def nl_split_runs(s):
    """Разбить по ПАЧКАМ переносов: «а\\n\\nб» — два сегмента, не три."""
    return [p for p in re.split(r"\n+", s)]


def nl_flat(s):
    """Текст без переносов + [(смещение в нём, пачка переносов)]."""
    out, buf, run = [], [], ""
    for ch in s:
        if ch == "\n":
            run += ch
        else:
            if run:
                out.append((len(buf), run))
                run = ""
            buf.append(ch)
    return "".join(buf), out


def nl_map_pos(blocks, p):
    """Перенести позицию из чужого текста в наш по блокам совпадения difflib."""
    best = None
    for i, j, n in blocks:
        if n == 0:
            continue
        if i <= p <= i + n:
            return j + (p - i)
        d = min(abs(p - i), abs(p - (i + n)))
        if best is None or d < best[0]:
            best = (d, j if p < i else j + n)
    return best[1] if best else None


def nl_snap_word(ru, i):
    """Не рвать слово пополам: подвинуть к ближайшей границе."""
    if i <= 0 or i >= len(ru):
        return None
    for d in range(6):
        for j in (i - d, i + d):
            if 0 < j < len(ru) and (ru[j - 1].isspace() or ru[j - 1] in ".!?…,;:»)"):
                return j
    return None


def nl_snap_glue(ru, i, win=12):
    """Склейка без пробела рядом — прямое свидетельство съеденного символа.

    Оценка по чужому переводу говорит лишь «примерно здесь», а склейка говорит
    «ровно здесь»: в живом тексте на этом месте обязан быть пробел.
    """
    best = None
    for j in range(max(1, i - win), min(len(ru), i + win)):
        if ru[j] in NL_HEAD and not ru[j - 1].isspace() and ru[j - 1] not in "«\"'([{<":
            d = abs(j - i)
            if best is None or d < best[0]:
                best = (d, j)
    return best[1] if best else i


def nl_by_oracle(en, ru, theirs, min_ratio=0.6, min_seg=0.45):
    """Вставить переносы по чужому переводу -> (новый текст, причина отказа)."""
    flat, brs = nl_flat(theirs)
    _f, enb = nl_flat(en)
    if not brs or len(brs) != len(enb):
        return None, "у оракула другое число разрывов"
    sm = difflib.SequenceMatcher(None, flat, ru, autojunk=False)
    if sm.ratio() < min_ratio:
        return None, "переводы слишком разные"
    blocks, cuts, cur = sm.get_matching_blocks(), [], 0
    for p, run in brs:
        m = nl_map_pos(blocks, p)
        s = nl_snap_word(ru, m) if m is not None else None
        if s is None:
            return None, "позиция не легла на границу слова"
        s = nl_snap_glue(ru, s)
        if s <= cur:
            return None, "швы сошлись в одну точку"
        cuts.append((s, run))
        cur = s
    parts, prev = [], 0
    for i, run in cuts:
        parts.append(ru[prev:i].rstrip())
        prev = i
    parts.append(ru[prev:].lstrip())
    new = "".join(p + r for p, (_i, r) in zip(parts, cuts)) + parts[-1]
    # сегменты должны отвечать сегментам оракула: иначе шов сел в середину фразы
    theirs_segs = nl_split_runs(theirs)
    if len(theirs_segs) != len(parts):
        return None, "сегменты оракула не бьются"
    for t_seg, our in zip(theirs_segs, parts):
        if difflib.SequenceMatcher(None, t_seg.strip(), our.strip()).ratio() < min_seg:
            return None, "сегмент не похож на оракульский"
    return new, None


def cmd_newlines(a):
    """Вернуть переводы строк, съеденные в переводе.

    Правим только колонку перевода: хеш и английский не трогаются, поэтому
    промахнуться мимо строки в игре нельзя. Берём лишь те вставки, которые
    подтвердил оригинал; «сверять нечего» (проза без чисел, тегов и маркеров)
    идёт отдельным счётом и применяется только с --weak.
    """
    ours = load_map(OUR_BIN)
    oracle = load_map(a.oracle) if a.oracle else {}
    changes, weak, stat = {}, {}, collections.Counter()
    ex, ex_or = [], []
    for h, (en, ru, _c) in ours.items():
        if not en or not ru or en.count("\n") <= ru.count("\n"):
            continue
        stat["всего с потерянными переносами"] += 1
        picks, q = nl_align(en, ru)
        if not picks:
            # своих следов шва нет — спросим чужой перевод той же строки
            theirs = oracle.get(h, ("", ""))[1] if oracle else ""
            if theirs and "\n" in theirs:
                new, why = nl_by_oracle(en, ru, theirs)
                if new:
                    good, checked = nl_verify(en, new)
                    if good and checked:
                        changes[h] = new
                        stat["  по оракулу, сверено с оригиналом"] += 1
                        if len(ex_or) < 4:
                            ex_or.append((en, ru, new))
                        continue
                    why = "приёмка по оригиналу отвергла" if good is False else \
                          "по оракулу, но сверять нечего"
                stat["  оракул: %s" % why] += 1
            stat["  шов не определён"] += 1
            continue
        if len(NL_WS.sub(" ", ru)) < 0.55 * len(NL_WS.sub(" ", en)):
            stat["  перевод короче — потерян сегмент"] += 1
            continue
        new = nl_apply(ru, picks)
        ok, checked = nl_verify(en, new)
        if not ok:
            stat["  оригинал не подтвердил"] += 1
            continue
        if checked:
            changes[h] = new
            stat["  подтверждено оригиналом (%s)" % q] += 1
            if len(ex) < 6:
                ex.append((en, ru, new))
        else:
            weak[h] = new
            stat["  сверять нечего, только позиция шва" ] += 1
    for k, v in sorted(stat.items()):
        print("%6d  %s" % (v, k))
    for en, ru, new in ex + ex_or:
        print("\n  EN    %r\n  было  %r\n  стало %r" % (en[:90], ru[:90], new[:90]))
    if a.weak:
        changes.update(weak)
        print("\n(--weak: добавлено %d вставок без сверки)" % len(weak))
    if a.apply:
        apply_changes(changes, {}, "возврат переносов")
    elif changes:
        print("\nготово к записи: %d (для записи --apply)" % len(changes))


# ------------------------------------------------------------ мелкие починки
# Пробельные символы по краям строки — часть склейки: игра лепит «Рецепт: » к
# названию, и лишний или потерянный пробел видно в интерфейсе. Перевод строки
# сюда НЕ входит: он про вёрстку, им занимается newlines.
EDGE_WS = " \t           " \
          "  　"
# Невидимки, которых в оригинале нет: пришли копипастой из вики и документов.
# Ломают поиск по строке и подстановку имён, на вид не отличаются ни от чего.
ZW = "​‌‍﻿­⁠‎‏"
LONE_PCT = re.compile(r"(?<!%)%(?!%)")


def tidy_edges(en, ru):
    """Края перевода привести к оригиналу: он эталон для склейки."""
    lead = en[:len(en) - len(en.lstrip(EDGE_WS))]
    trail = en[len(en.rstrip(EDGE_WS)):]
    new = lead + ru.strip(EDGE_WS) + trail
    return new if new != ru else None


def tidy_zw(en, ru):
    """Убрать невидимки, которых нет в оригинале (лишние экземпляры — тоже)."""
    new = ru
    for ch in ZW:
        keep = en.count(ch)
        if new.count(ch) <= keep:
            continue
        if keep == 0:
            new = new.replace(ch, "")
        else:                                  # оставить ровно столько, сколько в оригинале
            parts = new.split(ch)
            new = ch.join(parts[:keep + 1]) + "".join(parts[keep + 1:])
    return new if new != ru else None


def tidy_percent(en, ru):
    """Удвоить одиночный «%» там, где в оригинале «%%».

    В игре «%%» — это экранированный процент. Одиночный «%» движок примет за
    начало плейсхолдера и съест следующий символ.
    """
    need = en.count("%%") - ru.count("%%")
    if need <= 0:
        return None
    lone = [m.start() for m in LONE_PCT.finditer(ru)
            if not re.match(r"%\w+%", ru[m.start():])]
    # чиним только когда одиночных ровно столько, сколько не хватает: иначе гадание
    if len(lone) != need:
        return None
    new = ru
    for i in reversed(lone):
        new = new[:i] + "%%" + new[i + 1:]
    return new if new.count("%%") == en.count("%%") else None


def tidy_rename(en, ru):
    """%num1% вместо %num2%: потерян ровно один, лишний ровно один — место известно."""
    lost = collections.Counter(PH.findall(en)) - collections.Counter(PH.findall(ru))
    extra = collections.Counter(PH.findall(ru)) - collections.Counter(PH.findall(en))
    if sum(lost.values()) != 1 or sum(extra.values()) != 1:
        return None
    new = ru.replace(list(extra)[0], list(lost)[0], 1)
    return new if sorted(PH.findall(new)) == sorted(PH.findall(en)) else None


# Хвост оригинала: «.», «!», «?», «...», «…» — иногда с закрывающей кавычкой.
END_RUN = re.compile(r"([.!?…]+)\s*$")
# «Inquest Security Credentials Dept.» — точка от сокращения, а не от фразы:
# последнее слово с заглавной, короткое, и других знаков конца в строке нет.
ABBR_END = re.compile(r"\b[A-Z][A-Za-z]{0,4}\.\s*$")


def tidy_terminal(en, ru):
    """Дописать знак конца фразы, который есть в оригинале.

    Берём не «точку вообще», а ровно тот хвост, что стоит в оригинале: «...»
    останется многоточием, «?» — вопросом. Трогаем только переводы, которые
    кончаются буквой или цифрой: если там уже любой знак, это выбор переводчика.
    """
    m = END_RUN.search(en)
    if not m or not ru.rstrip():
        return None
    tail = ru.rstrip()[-1]
    if not (tail.isalpha() or tail.isdigit()):
        return None
    if ABBR_END.search(en) and not re.search(r"[.!?…]\s+\S", en):
        return None                            # точка от сокращения — не переносим
    if "\n" in ru:
        return None                            # многострочник: конец не там, где кажется
    if ru.lstrip().startswith("'") and not en.lstrip().startswith("'"):
        return None                            # огрызок от SQL-разбора — чинится не здесь
    return ru.rstrip() + m.group(1)


TIDY = (("края строки (лишний/потерянный пробел)", tidy_edges),
        ("невидимки, которых нет в оригинале", tidy_zw),
        ("одиночный %% вместо %%%%", tidy_percent),
        ("переименование плейсхолдера", tidy_rename),
        ("знак конца фразы из оригинала", tidy_terminal))


def cmd_tidy(a):
    """Мелкие механические починки перевода: края, невидимки, проценты, имена подстановок.

    Каждая правка проверяется на месте: набор служебных токенов после неё обязан
    остаться тем же (или сойтись с оригиналом — для процентов и переименования).
    Точки в конце фраз сюда не входят: там правка косметическая и массовая.
    """
    ours = load_map(OUR_BIN)
    changes, stat, ex = {}, collections.Counter(), collections.defaultdict(list)
    skipped_nl = 0
    for h, (en, ru, _c) in ours.items():
        if not en or not ru:
            continue
        if ru.strip(EDGE_WS)[-1:] == "\n" or ru[-1:] == "\n":
            skipped_nl += 1                    # хвостовой перенос — не наша забота
        cur = ru
        for name, fn in TIDY:
            new = fn(en, cur)
            if not new or new == cur:
                continue
            # токены не должны шевельнуться от починки пробелов и невидимок
            if fn in (tidy_edges, tidy_zw) and sorted(TOK.findall(new)) != sorted(TOK.findall(cur)):
                continue
            stat[name] += 1
            if len(ex[name]) < 3:
                ex[name].append((en, cur, new))
            cur = new
        if cur != ru:
            changes[h] = cur
    for name, _fn in TIDY:
        if stat[name]:
            print("%6d  %s" % (stat[name], name))
    print("%6d  записей к правке (хвостовой перенос пропущен у %d)" % (len(changes), skipped_nl))
    for name, _fn in TIDY:
        for en, was, new in ex[name]:
            print("\n  [%s]\n  EN    %r\n  было  %r\n  стало %r"
                  % (name, en[:80], was[:80], new[:80]))
    if a.apply:
        apply_changes(changes, {}, "мелкие починки")
    elif changes:
        print("\n(для записи --apply)")


# --------------------------------------------------- подписи писем в переводе
# «…helping our community.\n—Milin»: в оригинале письмо подписано, в переводе
# подписи нет вовсе. Это не потерянный символ, а потерянный сегмент, поэтому
# правка не расставляет швы, а дописывает строку — и только её.
# Пачку переносов берём целиком: перед подписью часто стоит пустая строка,
# и вернуть надо ровно её, а не один перенос.
SIGN = re.compile(r"(\n+)([ \t]*[—–-][ \t]*)([^\n]{2,40}?)((?:</c>)?)[ \t]*$")


def name_map():
    """{английское имя: русское} из слоя pn_* — канон проекта, а не наша выдумка."""
    out = {}
    for name, es in read_sections(OUR_BIN):
        if name.split("\x1f")[0].startswith("pn_"):
            for _h, en, ru in es:
                if en and ru:
                    out.setdefault(en, ru)
    return out


def cmd_signatures(a):
    """Вернуть подпись письма, потерянную переводом.

    Имя берём из слоя pn_*; если его там нет, оставляем латиницу — в игре её
    подставит тот же слой, а придумывать транслитерацию мимо канона нельзя.
    Хвостовой тег </c> сохраняем на месте: он обнимает всё письмо целиком.
    """
    ours = load_map(OUR_BIN)
    names = name_map()
    changes, kept_latin, ex = {}, 0, []
    for h, (en, ru, _c) in ours.items():
        if not en or not ru or en.count("\n") <= ru.count("\n"):
            continue
        m = SIGN.search(en)
        if not m:
            continue
        if re.search(r"[—–]\s*\S", ru.strip()[-45:]):
            continue                              # подпись на месте
        # Тело письма должно быть на месте: если перевод втрое короче оригинала,
        # потеряна не подпись, а половина текста — дописывать её некуда.
        body_en = len(NL_WS.sub(" ", en[:m.start()]))
        if len(NL_WS.sub(" ", ru)) < 0.6 * body_en:
            continue
        run, dash, who, tail = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
        ru_who = names.get(who)
        if not ru_who:
            kept_latin += 1
            ru_who = who
        body = ru.rstrip()
        if tail and body.endswith(tail):
            body = body[:-len(tail)].rstrip()     # </c> уедет за подпись
        new = body + run + dash.strip() + ru_who + tail
        changes[h] = new
        if len(ex) < 5:
            ex.append((en, ru, new))
    print("писем без подписи: %d (имя взято латиницей, канона нет: %d)"
          % (len(changes), kept_latin))
    for en, ru, new in ex:
        print("\n  EN    %r\n  было  %r\n  стало %r"
              % (en[-70:], ru[-70:], new[-70:]))
    if a.apply:
        apply_changes(changes, {}, "возврат подписей")
    elif changes:
        print("\n(для записи --apply)")


def cmd_trunc(a):
    now, base = load_map(OUR_BIN), load_map(a.baseline)
    sus = [(h, en, ru, cat) for h, (en, ru, cat) in now.items()
           if (h not in base or base[h][1] != ru) and looks_truncated(en, ru)]
    print("обрезанных при разборе CSV: %d" % len(sus))
    if not a.revert:
        print("(для отката --revert)")
        return
    changes = {h: (base[h][1] if h in base else "") for h, *_ in sus}
    changes = {h: v for h, v in changes.items() if v}
    apply_changes(changes, {}, "откат обрывов")


def cmd_overwrites(a):
    now, base = load_map(OUR_BIN), load_map(a.baseline)
    human = {fnv1a_u16(en) for en in human_pairs()}
    lost = hum = just = unj = 0
    reg = {}
    for h, (en, ru, _c) in base.items():
        if not ru.strip() or len(en) < a.min_len:
            continue
        cur = now.get(h)
        if cur is None:
            lost += 1
            continue
        if cur[1].strip() == ru.strip():
            continue
        if h in human:
            hum += 1
        elif defects(en, ru):
            just += 1
        else:
            unj += 1
        if defects(en, cur[1]) and not defects(en, ru):
            reg[h] = ru
    print("наши переводы длиннее %d символов:" % a.min_len)
    print("  ПОТЕРЯНО:                            %5d" % lost)
    print("  заменено, а это ручной перевод:      %5d   <- должно быть 0" % hum)
    print("  заменено, наш вариант был с дефектом:%5d   <- так и задумано" % just)
    print("  заменено, наш был чистый:            %5d   <- должно быть 0" % unj)
    print("регрессий (было чисто, стало с дефектом): %d" % len(reg))
    if reg and a.fix_regressions:
        apply_changes(reg, {}, "откат регрессий")
    elif reg:
        print("(для отката --fix-regressions)")


def cmd_restore(a):
    now, base = load_map(OUR_BIN), load_map(a.baseline)
    added = {h: (en, ru, cat) for h, (en, ru, cat) in base.items()
             if h not in now and ru.strip()}
    print("пропало из bin: %d" % len(added))
    if a.apply:
        apply_changes({}, added, "восстановление")
    elif added:
        print("(для записи --apply)")


def cmd_long(a):
    now, base = load_map(OUR_BIN), load_map(a.baseline)
    imported = [(h, en, ru) for h, (en, ru, _c) in now.items()
                if len(en) > a.min and (h not in base or base[h][1] != ru)]
    b = collections.Counter()
    for _h, en, ru in imported:
        e, r = SERVICE.sub("", en), SERVICE.sub("", ru)
        if looks_truncated(en, ru):
            b["обрыв"] += 1
        if len(re.findall(r"[.!?…]", e)) >= 3 and \
           len(re.findall(r"[.!?…]", r)) < len(re.findall(r"[.!?…]", e)) * 0.6:
            b["потеряны предложения"] += 1
        if _validate:
            errs = _validate.check_row(en, ru)
            if errs[0]:
                b["ошибки линтера"] += 1
            if any("ты" in w and "вы" in w for w in errs[1]):
                b["«ты» и «вы» вместе"] += 1
    print("строк длиннее %d символов: %d" % (a.min, len(imported)))
    for k, n in b.most_common():
        print("  %-26s %5d  (%.2f%%)" % (k, n, 100.0 * n / max(1, len(imported))))


def cmd_ratio(a):
    """Порог ±20 % бракует ~20 % наших же честных переводов — печатаем эталон,
    чтобы было видно, реалистичен ли выбранный допуск."""
    lim = a.max_diff / 100.0

    def scan(pairs, label):
        rs, over = [], []
        for en, ru in pairs:
            e, r = _body(en), _body(ru)
            if len(e) < a.min_len or not r:
                continue
            k = len(r) / len(e)
            rs.append(k)
            if abs(k - 1.0) > lim:
                over.append((k, en, ru))
        if rs:
            rs.sort()
            print("%s: строк %d | медиана %.2f | вне ±%d%%: %d (%.1f%%)"
                  % (label, len(rs), rs[len(rs) // 2], a.max_diff, len(over),
                     100.0 * len(over) / len(rs)))
        return over

    scan(list(human_pairs().items()), "ручные переводы батчей (эталон)")
    now = load_map(OUR_BIN)
    scan([(en, ru) for en, ru, _c in now.values()], "весь словарь")


def cmd_verify(a):
    now, base = load_map(OUR_BIN), load_map(a.baseline)
    lost = [h for h in base if h not in now]
    changed = [h for h in base if h in now and base[h][1] != now[h][1]]
    print("записей: %d -> %d (%+d)" % (len(base), len(now), len(now) - len(base)))
    print("потеряно из старого: %d" % len(lost))
    print("изменено переводов:  %d" % len(changed))
    blob = "\n".join(ru for _en, ru, _c in now.values())
    print("канон (должно быть 0):")
    for label, rx in (("скин", RX_SKIN.pattern), ("скайскал", r"скайскал"),
                      ("доляк", r"доляк"), ("Жайтан", r"Жайтан")):
        print("  %-10s %5d" % (label, len(re.findall(rx, blob))))


def cmd_fillbatches(a):
    """Закрыть пустые строки батчей переводами, которые уже есть в словаре."""
    if _validate is None:
        sys.exit("не найден crowdsource/validate.py — без линтера не заполняю")
    by_en = {}
    for _h, (en, ru, _c) in load_map(OUR_BIN).items():
        if en and ru.strip():
            by_en[en] = ru
    tot_empty = tot_fill = tot_rej = 0
    for fp in batch_files():
        raw = open(fp, "rb").read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(raw)))
        if not rows or rows[0][:1] != ["english"]:
            continue
        fill = 0
        for r in rows[1:]:
            if len(r) < 2:
                r += [""] * (2 - len(r))
            if not r[0].strip() or r[1].strip():
                continue
            tot_empty += 1
            ru = by_en.get(r[0])
            if not ru:
                continue
            if _validate.check_row(r[0], ru)[0]:
                tot_rej += 1
                continue
            r[1] = ru
            fill += 1
        if fill and a.apply:
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            open(fp, "wb").write(buf.getvalue().encode("utf-8"))
        tot_fill += fill
    print("пустых строк в батчах: %d | заполнено: %d | отклонено линтером: %d"
          % (tot_empty, tot_fill, tot_rej))
    if not a.apply:
        print("(для записи --apply)")


def unfix_by_english(en, ru):
    """Починка гомоглифов со сверкой по оригиналу.

    Соседи не помогают на самой границе склейки («…ИмператорВlood»): слева
    кириллица, справа латиница. Зато английский оригинал знает, как слово
    пишется. Меняем букву-двойник на латинскую, только если после замены
    вокруг неё складывается латинское слово, которое есть в оригинале.
    """
    en_words = set(re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", en))
    if not en_words:
        return ru
    chars = list(ru)
    changed = False
    for i, c in enumerate(chars):
        if c not in CYR2LAT:
            continue
        probe = chars[:]
        probe[i] = CYR2LAT[c]
        j = i
        while j > 0 and probe[j - 1].isascii() and probe[j - 1].isalpha():
            j -= 1
        k = i
        while k + 1 < len(probe) and probe[k + 1].isascii() and probe[k + 1].isalpha():
            k += 1
        word = "".join(probe[j:k + 1])
        # В оригинале слово тоже бывает склеено («…ImperatorBlood»), поэтому
        # отдельным словом «Blood» там не найдётся. Для слов от 4 букв
        # принимаем и вхождение подстрокой — случайных совпадений такой
        # длины в оригинале практически не бывает.
        if len(word) >= 3 and (word in en_words or (len(word) >= 4 and word in en)):
            chars = probe
            changed = True
    return "".join(chars) if changed else ru


def cmd_unglue(a):
    """Починить строки, испорченные прежним правилом гомоглифов.

    Старая версия fix_homoglyphs подменяла латинские буквы на кириллические
    в любом слове, где была хоть одна кириллическая. На склейках вида
    «RuinbringerИмператорBlood» это калечило английскую часть.
    """
    ours = load_map(OUR_BIN)
    changes = {}
    for h, (en, ru, _c) in ours.items():
        v = unfix_by_english(en, unfix_homoglyphs(ru))
        if v != ru:
            changes[h] = v
    print("строк с испорченной латиницей: %d" % len(changes))
    for h in list(changes)[:5]:
        print("  было:  %r" % ours[h][1][:80])
        print("  стало: %r" % changes[h][:80])
    if a.apply:
        apply_changes(changes, {}, "починка гомоглифов")
    elif changes:
        print("(для записи --apply)")


# ------------------------------------------- сбор канона имён из самого корпуса
# Слой pn_* — единственное место, где чинится написание имени. Всё, что течёт
# латиницей мимо него, приходится править в каждой строке отдельно. Пополняем
# слой не выдумкой, а тем, что уже переведено в корпусе.
CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
           "ж": "j", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
           "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
           "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
           "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "u", "я": "ya"}
PN_LEAK = re.compile(r"\b[A-Z][A-Za-z'’-]{2,}(?:\s+[A-Z][A-Za-z'’-]{2,}){0,2}\b")
PN_NAME = re.compile(r"^[A-Z][A-Za-z'’.-]*(?: [A-Z][A-Za-z'’.-]*){0,2}$")


def _ru2lat(s):
    return "".join(CYR2LAT.get(c, c if c.isalnum() else " ") for c in s.lower())


def _en_norm(s):
    s = s.lower()
    for a, b in (("ck", "k"), ("ph", "f"), ("th", "t"), ("qu", "kv"), ("c", "k"),
                 ("x", "ks"), ("w", "v"), ("y", "i"), ("ee", "i"), ("oo", "u"),
                 ("ai", "ei"), ("gh", "g"), ("'", ""), ("’", "")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9 ]", " ", s)


def translit_score(en, ru):
    """Насколько русское написание похоже на транслитерацию английского.

    Отделяет имя («Crecia» -> «Креция», 0.71) от смыслового перевода
    («Heart» -> «Сердце», 0.50): в слой имён годится только первое, иначе
    подстановка изуродует «Heart of Thorns» в каждой строке.
    """
    return difflib.SequenceMatcher(None, _en_norm(en), _ru2lat(ru)).ratio()


def cmd_pnharvest(a):
    """Собрать в слой имён то, что уже переведено в корпусе.

    Берём латиницу, которая осталась в наших переводах (её игрок видит как есть),
    и ищем для неё готовую пару «английская строка = имя, перевод кириллицей» в
    самом словаре. Принимаем два вида: транслитерация (порог --min-score) и
    составное название из двух-трёх слов, где перевод тоже составной — одиночное
    слово со смысловым переводом почти всегда кусок длинного названия.
    """
    ours = load_map(OUR_BIN)
    in_pn = set()
    for name, es in read_sections(OUR_BIN):
        if name.split("\x1f")[0].startswith("pn_"):
            in_pn.update(en for _h, en, _ru in es if en)

    leak = collections.Counter()
    for _h, (_en, ru, _c) in ours.items():
        if not ru:
            continue
        for m in PN_LEAK.finditer(re.sub(r"%\w+%|<[^>]+>", " ", ru)):
            if m.group(0) not in in_pn:
                leak[m.group(0)] += 1

    corpus = {}
    for _h, (en, ru, _c) in ours.items():
        if not en or not ru or len(en) > 34 or len(ru) > 40:
            continue
        if not PN_NAME.match(en) or not CYR.search(ru) or LAT.search(ru):
            continue
        corpus.setdefault(en, ru)

    rows, kinds = [], collections.Counter()
    for en, n in leak.most_common():
        ru = corpus.get(en)
        if not ru:
            continue
        sc = translit_score(en, ru)
        if sc >= a.min_score:
            kind = "транслитерация"
        elif " " in en and " " in ru:
            kind = "составное название"
        else:
            kinds["отклонено: одиночное слово со смысловым переводом"] += 1
            continue
        kinds[kind] += 1
        rows.append((en, ru, "pn_names", n, "%.2f" % sc, kind))

    print("течёт латиницей мимо слоя: %d цепочек" % len(leak))
    for k, v in kinds.most_common():
        print("  %-52s %5d" % (k, v))
    print("к добавлению: %d (покроют %d вхождений в переводах)"
          % (len(rows), sum(r[3] for r in rows)))
    for en, ru, _l, n, sc, kind in rows[:15]:
        print("   %-26s x%-4d -> %-24s %s %s" % (en, n, ru, sc, kind))
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["english", "translate", "layer", "вхождений", "сходство", "вид"])
        w.writerows(rows)
    print("\n-> %s  (влить: pnadd --file %s --apply)" % (a.out, os.path.basename(a.out)))


def cmd_pnadd(a):
    """Добавить имена собственные в слой pn_* из CSV «english,translate,layer».

    Слой pn_* нужен не только ради самих строк: по нему гейт понимает, что
    латиница в переводе — это имя, а не забытое слово. Чем полнее слой, тем
    меньше ложных отказов при вливании батчей.
    """
    fp = a.file or os.path.join(CROWD, "pn_additions.csv")
    if not os.path.isfile(fp):
        sys.exit("не найден %s" % fp)
    with open(fp, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f) if len(r) >= 3 and r[0].strip()]
    if rows and rows[0][0].strip().lower() == "english":
        rows = rows[1:]
    # Проверяем наличие именно в слое pn_*, а не во всём словаре: строка
    # может лежать в «основном» как обычный текст, но слою имён от этого
    # не легче — гейт смотрит только на pn_*.
    in_pn = set()
    for name, es in read_sections(OUR_BIN):
        if name.split("\x1f")[0].startswith("pn_"):
            in_pn.update(h for h, _en, _ru in es)
    added, exists = {}, 0
    for en, ru, layer in ((r[0].strip(), r[1].strip(), r[2].strip()) for r in rows):
        if not en or not ru:
            continue
        h = fnv1a_u16(en)
        if h in in_pn:
            exists += 1
            continue
        added[h] = (en, ru, layer)
    print("в файле: %d | добавить: %d | уже есть: %d" % (len(rows), len(added), exists))
    per = collections.Counter(v[2] for v in added.values())
    for lay, n in per.most_common():
        print("  %-16s %4d" % (lay, n))
    if a.apply:
        apply_changes({}, added, "слой имён")
    else:
        print("(для записи --apply)")


def cmd_export(a):
    """bin -> CSV. Нужна, если снова понадобится старый пайплайн."""
    os.makedirs(a.outdir, exist_ok=True)
    n = 0
    for name, es in read_sections(OUR_BIN):
        cat, _, disp = name.partition("\x1f")
        if cat == "основной":
            fn = "main_strings.csv"
        elif cat == "выученные":
            fn = "discovered_strings.csv"
        elif cat.startswith("pn_"):
            fn = cat + ".csv"
        else:
            fn = "dict_%s.csv" % cat
        rows = [["english", "translate"] + (["display"] if disp else [])]
        if disp:
            rows[0] = ["english", "translate", disp]
        for h, en, ru in es:
            rows.append([en if en else "%016x" % h, ru] + ([""] if disp else []))
        with open(os.path.join(a.outdir, fn), "w", encoding="utf-8", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows(rows)
        n += 1
    print("выгружено файлов: %d -> %s" % (n, a.outdir))


def main():
    ap = argparse.ArgumentParser(description="Инструмент словаря GlyphCore (работа с bin)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help, fn, *args):
        p = sub.add_parser(name, help=help)
        for arg, kw in args:
            p.add_argument(*arg, **kw)
        p.set_defaults(fn=fn)
        return p

    add("diff", "что есть у них и нет у нас", cmd_diff,
        (("foreign",), {}), (("--our",), {"default": OUR_BIN}))
    add("audit", "гейты качества по чужому словарю", cmd_audit,
        (("foreign",), {}), (("--our",), {"default": OUR_BIN}))
    add("merge", "влить чужой словарь", cmd_merge,
        (("foreign",), {}), (("--our",), {"default": OUR_BIN}),
        (("--apply",), {"action": "store_true"}))
    add("frombatches", "влить переводы батчей (замена merge_back)", cmd_frombatches,
        (("--apply",), {"action": "store_true"}))
    add("canon", "привести к канону терминов", cmd_canon,
        (("--apply",), {"action": "store_true"}))
    add("broken", "разбор битых строк", cmd_broken,
        (("--fix-br",), {"action": "store_true"}),
        (("--fix-tags",), {"action": "store_true"}))
    add("trunc", "обрывы от чужого CSV-разбора", cmd_trunc,
        (("baseline",), {}), (("--revert",), {"action": "store_true"}))
    add("overwrites", "что стало с нашими переводами", cmd_overwrites,
        (("baseline",), {}), (("--min-len",), {"type": int, "default": 0}),
        (("--fix-regressions",), {"action": "store_true"}))
    add("restore", "вернуть пропавшие переводы", cmd_restore,
        (("baseline",), {}), (("--apply",), {"action": "store_true"}))
    add("long", "проверка длинной прозы", cmd_long,
        (("baseline",), {}), (("--min",), {"type": int, "default": 100}))
    add("ratio", "длина русского против английского", cmd_ratio,
        (("--max-diff",), {"type": int, "default": 20}),
        (("--min-len",), {"type": int, "default": 25}))
    add("verify", "сверить с прежним bin", cmd_verify, (("baseline",), {}))
    add("fillbatches", "закрыть строки батчей переводами из bin", cmd_fillbatches,
        (("--apply",), {"action": "store_true"}))
    add("unglue", "починить гомоглифы в склейках", cmd_unglue,
        (("--apply",), {"action": "store_true"}))
    add("extratok", "убрать лишние токены из переводов", cmd_extratok,
        (("--apply",), {"action": "store_true"}))
    add("unquote", "воскресить записи с закавыченным английским", cmd_unquote,
        (("--apply",), {"action": "store_true"}))
    add("escapes", "развернуть литеральные \\n в переводах", cmd_escapes,
        (("--apply",), {"action": "store_true"}))
    add("newlines", "вернуть съеденные переводы строк", cmd_newlines,
        (("--weak",), {"action": "store_true", "help": "и вставки без сверки с оригиналом"}),
        (("--oracle",), {"default": None,
                         "help": "чужой bin: где следов шва нет, спросить его перевод"}),
        (("--apply",), {"action": "store_true"}))
    add("tidy", "мелкие починки: края, невидимки, %%, знак конца", cmd_tidy,
        (("--apply",), {"action": "store_true"}))
    add("enbroken", "огрызки английского: восстановить по батчам", cmd_enbroken,
        (("--apply",), {"action": "store_true"}))
    add("signatures", "вернуть потерянные подписи писем", cmd_signatures,
        (("--apply",), {"action": "store_true"}))
    add("pnharvest", "собрать канон имён из корпуса в слой pn_*", cmd_pnharvest,
        (("--out",), {"default": os.path.join(CROWD, "pn_harvest.csv")}),
        (("--min-score",), {"type": float, "default": 0.68}))
    add("pnadd", "добавить имена в слой pn_*", cmd_pnadd,
        (("--file",), {"default": None}), (("--apply",), {"action": "store_true"}))
    add("export", "выгрузить bin обратно в CSV", cmd_export, (("outdir",), {}))

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
