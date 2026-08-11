#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Готовит релиз русификатора: проверяет glyphCore/dictionary.bin и печатает
команду публикации. Архив не собирается — ассет релиза один: сам
dictionary.bin. Инструкция по установке живёт в README.md.

    python make_release.py                 # версия = дата (v2026.08.03)
    python make_release.py v2026.08.03     # свой тег
    python make_release.py --no-lint       # не гонять линтер по словарю
    python make_release.py --lint-details  # показать примеры найденных линтом ошибок

Источник истины — БАТЧИ, а не bin: они версионируются в git, bin нет. Перед
релизом bin пересобирается из батчей (см. README, «Порядок работы»):

    python crowdsource/tools/dict_tool.py frombatches --batches-win --apply
    python crowdsource/tools/index.py build        # пересобрать граф

Руками bin не правят. Исключение — то, чего в батчах нет: слой имён (`pnadd`,
`pnset`) и русские имена словарей (`dictnames`).

Флаг --no-build оставлен, чтобы старые вызовы не падали, но ничего не делает.
"""
import os, sys, csv, glob, struct, datetime, hashlib, collections

HERE  = os.path.dirname(os.path.abspath(__file__))        # crowdsource
GCDIR = os.path.dirname(HERE)                             # glyphCore
BIN   = os.path.join(GCDIR, "dictionary.bin")
MAGIC = b"GCDCT2"

sys.path.insert(0, HERE)
import validate                                            # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
csv.field_size_limit(1 << 30)


def num(n):
    return f"{n:,}".replace(",", " ")


def read_header(path):
    """Заголовок GCDCT2 -> (всего пар, [(категория, пар), ...])."""
    with open(path, "rb") as f:
        head = f.read(1 << 16)
    if head[:6] != MAGIC:
        raise ValueError(f"не словарь GlyphCore: сигнатура {head[:6]!r}, ожидалась {MAGIC.decode()}")
    p = 8
    total, = struct.unpack_from("<I", head, p); p += 4
    ncat,  = struct.unpack_from("<H", head, p); p += 2
    cats = []
    for _ in range(ncat):
        ln = head[p]; p += 1
        name = head[p:p + ln].decode("utf-8", "replace"); p += ln
        cnt, = struct.unpack_from("<I", head, p); p += 4
        p += 8
        cats.append((name, cnt))
    return total, cats


def read_entries(path):
    """GCDCT2 -> [(имя категории, [(hash, english, русский), ...]), ...]."""
    with open(path, "rb") as f:
        b = f.read()
    if b[:6] != MAGIC:
        raise ValueError(f"сигнатура {b[:6]!r}, ожидалась {MAGIC.decode()}")
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
            q += 4
            h, = struct.unpack_from("<Q", b, q); q += 8
            l, = struct.unpack_from("<H", b, q); q += 2
            en = b[q:q + l * 2].decode("utf-16-le", "replace"); q += l * 2
            l, = struct.unpack_from("<H", b, q); q += 2
            ru = b[q:q + l * 2].decode("utf-16-le", "replace"); q += l * 2
            es.append((h, en, ru))
        out.append((name, es))
    return out


def check_bin():
    """Статистика по самому bin. Возвращает список предупреждений.

    CSV-слой удалён, поэтому сверять bin больше не с чем: он и есть источник.
    Проверяем то, что видно изнутри файла."""
    warn = []
    total, cats = read_header(BIN)
    print(f"\ndictionary.bin: {os.path.getsize(BIN) / 1_048_576:.1f} МБ | "
          f"категорий: {len(cats)} | пар: {num(total)}")
    print(f"  изменён: {datetime.datetime.fromtimestamp(os.path.getmtime(BIN)):%Y-%m-%d %H:%M}")

    names = {n.split("\x1f")[0] for n, _ in cats}
    top = sorted(cats, key=lambda c: -c[1])[:6]
    print("  крупнейшие категории: "
          + ", ".join(f"{n.split(chr(31))[0]} {num(c)}" for n, c in top))

    if not any(n.startswith("pn_") for n in names):
        warn.append("в bin нет категорий pn_* — имена и локации останутся английскими.")
    if "основной" not in names:
        warn.append("в bin нет категории «основной» — это основной слой перевода.")
    return warn


def lint(details):
    """Линтер по содержимому bin. Ошибки не блокируют релиз (они уже в игре),
    но их видно до публикации. Возвращает список предупреждений.

    Раньше читал dict_*.csv; CSV-слоя больше нет, поэтому идём по самому bin.
    Записи «только по хешу» (english пустой) пропускаем: сравнивать не с чем."""
    kinds, examples, checked, bad = collections.Counter(), {}, 0, 0
    for name, entries in read_entries(BIN):
        cat = name.split("\x1f")[0]
        for h, en, ru in entries:
            if not en or not ru.strip():
                continue
            checked += 1
            errs, _ = validate.check_row(en, ru)
            if errs:
                bad += 1
            for m in errs:
                k = m.split(":")[0].split("«")[0].strip()
                kinds[k] += 1
                examples.setdefault(k, (cat, h, en[:70], ru[:70]))
    print(f"\nЛинт словаря: проверено {num(checked)} строк, "
          f"битых {num(bad)}")
    for k, c in kinds.most_common():
        print(f"  {num(c):>7}  {k}")
        if details:
            cat, h, en, ru = examples[k]
            print(f"           [{cat}] hash={h:016x}\n             EN {en!r}\n             RU {ru!r}")
    if not details and kinds:
        print("  (примеры: python make_release.py --lint-details)")
    if bad:
        return [f"линт нашёл {num(bad)} битых строк в словаре — "
                f"они попадут в релиз как есть (разбор: crowdsource/tools/dict_tool.py broken)."]
    return []


def main():
    ver = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if not ver:
        ver = "v" + datetime.date.today().strftime("%Y.%m.%d")

    if not os.path.exists(BIN):
        print(f"[!] {BIN} не найден.")
        return 1
    if "--no-build" not in sys.argv:
        # CSV-слоя больше нет: bin — источник, а не результат сборки.
        # Правки вносит crowdsource/tools/dict_tool.py, он же и записывает bin.
        print("Сборка из CSV больше не выполняется: CSV-слой удалён, "
              "dictionary.bin сам является источником.")
        print("  правки: crowdsource/tools/dict_tool.py (frombatches, canon, broken, merge)")
        print("  выгрузить обратно в CSV: crowdsource/tools/dict_tool.py export <папка>")

    try:
        warn = check_bin()
    except ValueError as e:
        print(f"[!] dictionary.bin битый: {e}")
        return 1

    if "--no-lint" not in sys.argv:
        warn += lint("--lint-details" in sys.argv)

    with open(BIN, "rb") as f:
        sha = hashlib.sha256()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    print(f"\nАссет релиза: {BIN}")
    print(f"  sha256: {sha.hexdigest()}")

    if warn:
        print("\nВНИМАНИЕ:")
        for w in warn:
            print(f"  ! {w}")

    print("\nОпубликовать (crowdsource — public repo):")
    print("  1) git push origin main")
    print("  2а) через gh CLI:")
    print(f'      gh release create {ver} "{BIN}" -t "Русификатор GW2 {ver}" '
          f'-n "Словарь на {ver}. Установка: см. README."')
    print("  2б) без gh — через сайт:")
    print("      открой https://github.com/K13or/crowdsource/releases/new")
    print(f"      тег {ver}, перетащи в ассеты: {BIN}")
    print("\nИнструкция для игроков — в README.md (раздел «Установка русификатора»);")
    print("в описании релиза достаточно ссылки на него.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
