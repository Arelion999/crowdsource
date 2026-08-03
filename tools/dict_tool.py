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
"""
import argparse, collections, csv, glob, io, os, re, shutil, struct, sys, time

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
    add("pnadd", "добавить имена в слой pn_*", cmd_pnadd,
        (("--file",), {"default": None}), (("--apply",), {"action": "store_true"}))
    add("export", "выгрузить bin обратно в CSV", cmd_export, (("outdir",), {}))

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
