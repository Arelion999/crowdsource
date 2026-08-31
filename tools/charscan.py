#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
charscan.py — невидимые символы и пунктуация в dictionary.bin.

Английская строка в bin — это оригинал из игры, русская — наш перевод. Значит
любой служебный символ оригинала (перевод строки, неразрывный пробел, токен
движка, плейсхолдер) можно потребовать и от перевода: игра рисует их сама, и
если символ потерялся, ломается вёрстка строки, а не только её вид.

Команды
-------
    inventory                       инвентарь: какие невидимые символы и знаки
                                    препинания вообще есть в EN и в RU, сколько
                                    их и в скольких записях они пропали
    check [--report f.csv]          разбор потерь по классам, с примерами
    compare <прежний.bin>           что сломалось (и что починилось) между
                                    двумя сборками — гейт для merge/canon/fix

Ключи всех команд:
    --bin <файл>             проверять не наш dictionary.bin, а этот

Ключи check/compare:
    --only <класс[,класс]>   только эти классы (имена — из сводки check)
    --limit N                примеров на класс в выводе (по умолчанию 5)
    --dead                   считать и мёртвые записи (см. ниже)
    --report <файл>          выгрузить все находки в CSV

МЁРТВЫЕ ЗАПИСИ. Хеш записи считается от английской строки; если english в bin
испорчен, хеш от него не совпадает с игровым, и в игре запись не всплывёт
никогда. Дефекты в таких записях чинить бессмысленно, поэтому по умолчанию они
считаются отдельной строкой сводки и в классы не попадают.

Ничего не пишет в bin: это глаза, а не руки. Починки — в dict_tool.py.

Итоги обеих команд живут в `crowdsource/DEFECTS.md`: класс, сколько было, сколько
осталось, состояние («убрано / убрано частично / не тронуто») и чем чинили. После
каждого прогона со свежими числами реестр надо обновить — правила в конце файла.
"""
import argparse, collections, csv, os, re, sys
import unicodedata as ud

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dict_tool import (EN_SWALLOWED, OUR_BIN, ROOT,             # noqa: E402
                       fnv1a_u16, read_sections)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ------------------------------------------------------------------ разметка
PH = re.compile(r"%\w+%")                       # %num1%, %str2% — подстановки игры
TAG = re.compile(r"<[^>]+>")                    # <c=@flavor>, </c>, <center>
TOKEN = re.compile(r"\[(?:lbracket|rbracket|null|plur|nosep|topic-[fm]|f)\]")
# [an]/[the] — артикль-токены движка, и в русском раскрывать их не во что.
# Их пропажа в переводе законна (корпус так и делает в 11 записях из 16), а вот
# наличие — дефект: игрок увидит «Открытие [the] Сундук». Пустые «[]» туда же:
# слово из скобок убрали, скобки забыли.
ARTICLE_RU = re.compile(r"\[(?:an|the)\]|\[\s*\]")
PLURAL_EN = re.compile(r"\[s\]|\[pl:\"[^\"]*\"\]")   # склонение по числу в оригинале
PLURAL_RU = re.compile(r"\[[^\]\[]*\|[^\]\[]*\]")    # [форма1|форма2|форма3]
PREFIX_EN = re.compile(r"^[^:\n]{2,30}:\s")     # «Рецепт: », «Aurene: » — структурный префикс
END_OK = ".!?…»\"')%]>*"                        # чем перевод имеет право кончаться
# EN_SWALLOWED (след плейсхолдера, выпавшего из самого оригинала) берём из
# dict_tool: по нему же там ищут огрызки для восстановления по батчам, и два
# определения разъехались бы.

# Невидимое: всё, что занимает место (или не занимает), но не рисуется. Перевод
# строки разбираем отдельным классом — он важнее всех остальных вместе взятых.
def is_invisible(ch):
    if ch in "\n\r\t":
        return False
    return ud.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") or \
        (ud.category(ch) == "Zs" and ch != " ")


def uname(ch):
    return "U+%04X %s" % (ord(ch), ud.name(ch, "?"))


def multiset_lost(en, ru, rx):
    """Чего из rx было в оригинале и не хватает в переводе."""
    a, b = collections.Counter(rx.findall(en)), collections.Counter(rx.findall(ru))
    return a - b


# ------------------------------------------------------------------- классы
# (класс, серьёзность, что проверяем). Функция возвращает None или пояснение.
# Серьёзность: «ошибка» — игра покажет не то, что задумано; «предупреждение» —
# страдает только вид текста; «инфо» — расхождение стиля, чинить необязательно.

def c_newline(en, ru):
    a, b = en.count("\n"), ru.count("\n")
    if a > b:
        return "переводов строки: %d -> %d" % (a, b)


def c_newline_extra(en, ru):
    a, b = en.count("\n"), ru.count("\n")
    if b > a:
        return "лишних переводов строки: %d -> %d" % (a, b)


def c_placeholder(en, ru):
    lost = multiset_lost(en, ru, PH)
    extra = multiset_lost(ru, en, PH)
    if not lost and (not extra or EN_SWALLOWED.search(en)):
        return None                       # лишнее при огрызке оригинала — класс en-broken
    s = []
    if lost:
        s.append("нет " + " ".join("%s×%d" % (k, v) for k, v in lost.items()))
    if extra:
        s.append("лишние " + " ".join("%s×%d" % (k, v) for k, v in extra.items()))
    return "; ".join(s)


def c_en_broken(en, ru):
    """Плейсхолдер потерял сам ОРИГИНАЛ, а не перевод.

    «Harvest plants  times» — на месте %num1% остался двойной пробел, и перевод
    честно держит %num1%. Хеш записи посчитан от этого огрызка, поэтому в игре
    она не всплывёт никогда: чинить надо английский и хеш (dict_tool unquote/
    frombatches), правкой перевода делу не поможешь.
    """
    extra = multiset_lost(ru, en, PH)
    if extra and not multiset_lost(en, ru, PH) and EN_SWALLOWED.search(en):
        return "оригинал потерял " + " ".join("%s×%d" % (k, v) for k, v in extra.items())


def c_percent(en, ru):
    # «%%» — экранированный процент; потеряли его — игра съест следующий символ
    # как начало плейсхолдера. Лишний «%%» в переводе смотрим только там, где
    # оригинал цел: иначе это тот же огрызок из en-broken.
    a, b = en.count("%%"), ru.count("%%")
    if a > b:
        return "%%%%: %d -> %d" % (a, b)
    if b > a and not EN_SWALLOWED.search(en):
        return "лишних %%%%: %d -> %d" % (a, b)


def c_tag(en, ru):
    lost = multiset_lost(en, ru, TAG)
    if lost:
        return "нет " + " ".join("%s×%d" % (k, v) for k, v in lost.items())


def c_token(en, ru):
    lost = multiset_lost(en, ru, TOKEN)
    if lost:
        return "нет " + " ".join("%s×%d" % (k, v) for k, v in lost.items())


def c_article(en, ru):
    hit = ARTICLE_RU.findall(ru)
    if hit:
        return "артикль-токен оставлен в русском: " + " ".join(sorted(set(hit)))


def c_plural(en, ru):
    # [s]/[pl:"…"] в оригинале обязан превратиться в группу [форма|форма|форма];
    # если групп нет вовсе — склонение потеряно, игра напишет одну форму на все числа.
    if PLURAL_EN.search(en) and not PLURAL_RU.search(ru):
        return "склонение по числу потеряно"


def c_invisible_lost(en, ru):
    a = collections.Counter(c for c in en if is_invisible(c))
    b = collections.Counter(c for c in ru if is_invisible(c))
    lost = a - b
    if lost:
        return "нет " + ", ".join("%s ×%d" % (uname(k), v) for k, v in lost.items())


def c_invisible_extra(en, ru):
    # Пришло из копипасты (вики, документы): в оригинале символа нет, в переводе
    # есть. Zero-width ломает поиск по строке и склейку имён.
    a = collections.Counter(c for c in en if is_invisible(c))
    b = collections.Counter(c for c in ru if is_invisible(c))
    extra = b - a
    if extra:
        return "лишние " + ", ".join("%s ×%d" % (uname(k), v) for k, v in extra.items())


def c_edge_space(en, ru):
    # Краевой пробел в оригинале — часть склейки («Рецепт: » + название).
    out = []
    if en[:1].isspace() and not ru[:1].isspace():
        out.append("потерян пробел в начале")
    if en[-1:].isspace() and not ru[-1:].isspace():
        out.append("потерян пробел в конце")
    if ru[:1].isspace() and not en[:1].isspace():
        out.append("лишний пробел в начале")
    if ru[-1:].isspace() and not en[-1:].isspace():
        out.append("лишний пробел в конце")
    if out:
        return ", ".join(out)


def c_terminal(en, ru):
    e, r = en.rstrip(), ru.rstrip()
    if e[-1:] in ".!?…" and r[-1:] not in END_OK:
        return "оригинал кончается «%s», перевод — «%s»" % (e[-1:], r[-1:])


def c_colon_prefix(en, ru):
    if PREFIX_EN.match(en) and ":" not in ru:
        return "потерян префикс «%s»" % PREFIX_EN.match(en).group().strip()


def c_brackets(en, ru):
    # Скобки оригинала — уточнение («(мастер)»), их выбрасывают вместе с текстом.
    a, b = en.count("("), ru.count("(")
    if a > b:
        return "круглых скобок: %d -> %d" % (a, b)


MARKER_Q = re.compile(r'\[(?:pl|f|pf|pm):"[^"]*"\]')


def c_quotes(en, ru):
    # Кавычки в русском другие («ёлочки»), поэтому смотрим не вид, а факт: были
    # кавычки — должны остаться хоть какие-то. Прямая речь — исключение: русская
    # традиция начинает реплику тире, и это не потеря, а перевод оформления.
    #
    # Разметку движка вырезаем ДО счёта: в `Box[pl:"Boxes"]` кавычки принадлежат
    # маркеру форм, а не тексту, и перевод их не обязан сохранять. Без этого
    # честный перевод «[Коробка|Коробки|Коробок] лёгкой брони асур» числился
    # потерей кавычек.
    en = MARKER_Q.sub("", en)
    if re.search(r'["“”«»„]', en) and not re.search(r'["“”«»„]', ru) \
            and not re.search(r'(?:^|\n)\s*[—–-]\s', ru):
        return "кавычки потеряны"


def c_bullet(en, ru):
    if en.count("•") > ru.count("•"):
        return "маркеров списка: %d -> %d" % (en.count("•"), ru.count("•"))


def c_replacement(en, ru):
    if "�" in ru:
        return "U+FFFD ×%d (битая кодировка)" % ru.count("�")


def c_ellipsis(en, ru):
    if "…" in en and "…" not in ru and "..." in ru:
        return "… -> ..."
    if "…" not in en and "..." in en and "…" in ru:
        return "... -> …"


CHECKS = [
    ("newline",         "ошибка",         c_newline),
    ("placeholder",     "ошибка",         c_placeholder),
    ("en-broken",       "ошибка",         c_en_broken),
    ("tag",             "ошибка",         c_tag),
    ("token",           "ошибка",         c_token),
    ("article",         "ошибка",         c_article),
    ("plural",          "ошибка",         c_plural),
    ("percent",         "ошибка",         c_percent),
    ("replacement",     "ошибка",         c_replacement),
    ("edge-space",      "ошибка",         c_edge_space),
    ("colon-prefix",    "предупреждение", c_colon_prefix),
    ("terminal",        "предупреждение", c_terminal),
    ("invisible-lost",  "предупреждение", c_invisible_lost),
    ("invisible-extra", "предупреждение", c_invisible_extra),
    ("newline-extra",   "предупреждение", c_newline_extra),
    ("brackets",        "предупреждение", c_brackets),
    ("quotes",          "предупреждение", c_quotes),
    ("bullet",          "предупреждение", c_bullet),
    ("ellipsis",        "инфо",           c_ellipsis),
]
SEVERITY = {n: s for n, s, _ in CHECKS}


# --------------------------------------------------------------------- сбор
def pairs(path, keep_dead=False):
    """[(категория, hash, en, ru)] по сравнимым записям + счётчики пропущенных.

    Пропускаем записи без английского («только по хешу» — сравнивать не с чем)
    и, если не просили обратного, мёртвые: у них хеш не от их же english.
    """
    out, skipped = [], collections.Counter()
    for name, es in read_sections(path):
        cat = name.split("\x1f")[0]
        for h, en, ru in es:
            if not en or not ru:
                skipped["без английского или без перевода"] += 1
                continue
            if not keep_dead and fnv1a_u16(en) != h:
                skipped["мёртвые (хеш не от своего english)"] += 1
                continue
            out.append((cat, h, en, ru))
    return out, skipped


def scan(rows, only=None):
    """{класс: [(категория, hash, en, ru, деталь)]}."""
    found = collections.defaultdict(list)
    checks = [c for c in CHECKS if not only or c[0] in only]
    for cat, h, en, ru in rows:
        for name, _sev, fn in checks:
            d = fn(en, ru)
            if d:
                found[name].append((cat, h, en, ru, d))
    return found


def cut(s, n=95):
    return repr(s[:n] + ("…" if len(s) > n else ""))


def report_csv(path, found):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["класс", "серьёзность", "категория", "hash", "english", "русский", "деталь"])
        for name, _sev, _fn in CHECKS:
            for cat, h, en, ru, d in found.get(name, []):
                w.writerow([name, SEVERITY[name], cat, "%016x" % h, en, ru, d])
    return sum(len(v) for v in found.values())


# ------------------------------------------------------------------ команды
def cmd_inventory(a):
    rows, skipped = pairs(a.bin, a.dead)
    en_cnt, ru_cnt = collections.Counter(), collections.Counter()
    en_rows, ru_rows = collections.Counter(), collections.Counter()
    lost_rows, extra_rows = collections.Counter(), collections.Counter()
    interest = lambda ch: (ch in "\n\r\t" or is_invisible(ch)
                           or ud.category(ch).startswith("P") or ch == " ")
    for _cat, _h, en, ru in rows:
        se = collections.Counter(c for c in en if interest(c))
        sr = collections.Counter(c for c in ru if interest(c))
        for ch, n in se.items():
            en_cnt[ch] += n; en_rows[ch] += 1
            if sr.get(ch, 0) == 0:
                lost_rows[ch] += 1
        for ch, n in sr.items():
            ru_cnt[ch] += n; ru_rows[ch] += 1
            if se.get(ch, 0) == 0:
                extra_rows[ch] += 1
    print("сравнимых пар: %s" % f"{len(rows):,}")
    for k, v in skipped.most_common():
        print("  пропущено: %-40s %s" % (k, f"{v:,}"))
    print("\n%-8s %-4s %12s %12s %10s %10s %10s %10s  %s"
          % ("символ", "кат", "en всего", "ru всего", "en строк", "ru строк",
             "нет в ru", "нет в en", "имя"))
    for ch in sorted(set(en_cnt) | set(ru_cnt), key=lambda c: -(en_cnt[c] + ru_cnt[c])):
        print("U+%04X   %-4s %12s %12s %10s %10s %10s %10s  %s"
              % (ord(ch), ud.category(ch), f"{en_cnt[ch]:,}", f"{ru_cnt[ch]:,}",
                 f"{en_rows[ch]:,}", f"{ru_rows[ch]:,}",
                 f"{lost_rows[ch]:,}", f"{extra_rows[ch]:,}",
                 ud.name(ch, "<без имени>")))


def cmd_check(a):
    only = set(a.only.split(",")) if a.only else None
    rows, skipped = pairs(a.bin, a.dead)
    found = scan(rows, only)
    print("проверено пар: %s" % f"{len(rows):,}")
    for k, v in skipped.most_common():
        print("  пропущено: %-40s %s" % (k, f"{v:,}"))
    err = sum(len(found[n]) for n, s, _ in CHECKS if s == "ошибка")
    warn = sum(len(found[n]) for n, s, _ in CHECKS if s == "предупреждение")
    print("\nнаходок: ошибок %s, предупреждений %s\n" % (f"{err:,}", f"{warn:,}"))
    print("%-16s %-14s %8s" % ("класс", "серьёзность", "записей"))
    for name, sev, _fn in CHECKS:
        if found.get(name):
            print("%-16s %-14s %8s" % (name, sev, f"{len(found[name]):,}"))
    for name, sev, _fn in CHECKS:
        hits = found.get(name)
        if not hits:
            continue
        print("\n=== %s (%s): %s" % (name, sev, f"{len(hits):,}"))
        by_cat = collections.Counter(c for c, *_ in hits)
        print("    по категориям: " + ", ".join("%s %d" % kv for kv in by_cat.most_common(6)))
        for cat, _h, en, ru, d in hits[:a.limit]:
            print("  [%s] %s" % (cat, d))
            print("      EN %s" % cut(en))
            print("      RU %s" % cut(ru))
    if a.report:
        n = report_csv(a.report, found)
        print("\nвыгружено находок: %s -> %s" % (f"{n:,}", os.path.relpath(a.report, ROOT)))
    return 0


def cmd_compare(a):
    """Сравнить дефекты двух сборок: что появилось, что исчезло.

    Сравниваем по хешу записи и по классу: запись, у которой класс появился, —
    регрессия (её перевод правили и сломали), исчез — починка. Разное число
    записей в сборках при этом не мешает: пропавшие записи считаются отдельно.
    """
    only = set(a.only.split(",")) if a.only else None
    new_rows, _ = pairs(a.bin, a.dead)
    old_rows, _ = pairs(a.baseline, a.dead)
    new_found, old_found = scan(new_rows, only), scan(old_rows, only)
    new_ru = {h: ru for _c, h, _en, ru in new_rows}
    old_ru = {h: ru for _c, h, _en, ru in old_rows}

    print("прежний: %s (%s пар) -> текущий: %s (%s пар)\n"
          % (os.path.basename(a.baseline), f"{len(old_rows):,}",
             os.path.basename(a.bin), f"{len(new_rows):,}"))
    print("%-16s %10s %10s %8s %10s %10s" %
          ("класс", "было", "стало", "дельта", "регрессий", "починок"))
    regress = {}
    for name, _sev, _fn in CHECKS:
        if only and name not in only:
            continue
        old_hits, new_hits = old_found.get(name, []), new_found.get(name, [])
        # Один хеш может лежать в двух категориях; для «было/стало» считаем
        # находки, для регрессий — записи, поэтому ключуем по хешу.
        o = {h: (cat, en, ru, d) for cat, h, en, ru, d in old_hits}
        n = {h: (cat, en, ru, d) for cat, h, en, ru, d in new_hits}
        # регрессия — только там, где запись была в обеих сборках: иначе это
        # не «сломали», а «влили новую строку с дефектом».
        reg = [h for h in n if h not in o and h in old_ru]
        fix = [h for h in o if h not in n and h in new_ru]
        if not (o or n):
            continue
        print("%-16s %10s %10s %8s %10s %10s"
              % (name, f"{len(old_hits):,}", f"{len(new_hits):,}",
                 "%+d" % (len(new_hits) - len(old_hits)),
                 f"{len(reg):,}", f"{len(fix):,}"))
        if reg:
            # (hash, категория, english, было, стало, деталь)
            regress[name] = [(h,) + (n[h][0], n[h][1], old_ru[h], n[h][2], n[h][3])
                             for h in reg]
    for name, items in regress.items():
        print("\n=== регрессии %s: %s" % (name, f"{len(items):,}"))
        for _h, cat, en, was, ru, d in items[:a.limit]:
            print("  [%s] %s" % (cat, d))
            print("      EN    %s" % cut(en))
            print("      было  %s" % cut(was))
            print("      стало %s" % cut(ru))
    if a.report:
        os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
        with open(a.report, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["класс", "категория", "hash", "english", "было", "стало", "деталь"])
            for name, items in regress.items():
                for h, cat, en, was, ru, d in items:
                    w.writerow([name, cat, "%016x" % h, en, was, ru, d])
        print("\nрегрессии выгружены -> %s" % os.path.relpath(a.report, ROOT))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Невидимые символы и пунктуация в dictionary.bin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help, fn, *args):
        p = sub.add_parser(name, help=help)
        for arg, kw in args:
            p.add_argument(*arg, **kw)
        p.add_argument("--bin", default=OUR_BIN, help="проверяемый bin (по умолчанию наш)")
        p.add_argument("--dead", action="store_true", help="считать и мёртвые записи")
        p.add_argument("--report", default=None, help="выгрузить находки в CSV")
        p.set_defaults(fn=fn)
        return p

    look = ((("--only",), {"default": None, "help": "только эти классы, через запятую"}),
            (("--limit",), {"type": int, "default": 5, "help": "примеров на класс"}))
    add("inventory", "инвентарь символов в EN и RU", cmd_inventory)
    add("check", "разбор потерь по классам", cmd_check, *look)
    add("compare", "что сломалось между сборками", cmd_compare,
        (("baseline",), {"help": "прежний bin"}), *look)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
