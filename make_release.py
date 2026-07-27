#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пакует локализацию из glyphCore/ в архив для GitHub-релиза (русификатор).

    python make_release.py                 # версия = дата (v2026.07.27)
    python make_release.py v2026.07.27     # своя версия/тег
    python make_release.py --csv           # старый формат: россыпь CSV вместо bin

По умолчанию собирает bin-релиз: dictionary.bin + pn_*.csv + config.ini + INSTALL.txt.
dictionary.bin собирается НЕ этим скриптом, а самим прокси: оверлей → «Файлы перевода»
→ кнопка «CSV в bin». Скрипт только проверяет готовый bin и пакует его.

pn_*.csv кладутся в архив отдельно: слой имён собственных в bin не сворачивается
(проверяется по списку категорий — если однажды свернётся, скрипт это заметит и скажет).

Результат: <родитель glyphCore>/gw2-ru-<версия>.zip — его прикрепляют ассетом к релизу.
Печатает готовые команды для публикации.
"""
import os, sys, csv, glob, struct, zipfile, datetime, hashlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE   = os.path.dirname(os.path.abspath(__file__))       # crowdsource
GCDIR  = os.path.dirname(HERE)                            # glyphCore
OUTDIR = os.path.dirname(GCDIR)                           # родитель (…/Guild Wars 2)

BIN    = os.path.join(GCDIR, "dictionary.bin")
MAGIC  = b"GCDCT2"

INSTALL_BIN = """Русификатор Guild Wars 2 (GlyphCore)

Установка:
1. Установи прокси-локализатор GlyphCore: https://github.com/nourlie/GlyphCore
2. Скопируй файлы из этого архива в папку glyphCore/ (заменив существующие).
3. Если в папке остались старые словари от прежних версий русификатора
   (main_strings.csv и dict_*.csv) — удали их. Они больше не нужны: весь словарь
   лежит в dictionary.bin, а старые CSV прокси всё равно не читает, когда есть bin.
4. Запусти игру через GlyphCore.

Содержимое:
  dictionary.bin — весь словарь одним файлом (переводы + выученные строки)
  pn_*.csv       — слой имён собственных (люди, локации, термины); в bin не входит
  config.ini     — настройки прокси; если ты уже настраивал свой, можешь не заменять
"""

INSTALL_CSV = """Русификатор Guild Wars 2 (GlyphCore)

Установка:
1. Установи прокси-локализатор GlyphCore: https://github.com/nourlie/GlyphCore
2. Скопируй файлы из этого архива в папку glyphCore/ (заменив существующие).
3. Запусти игру через GlyphCore.

Содержимое: main_strings.csv, dict_*.csv, pn_*.csv, config.ini.
"""


def num(n):
    """12345 → «12 345» (неразрывать не надо, это консоль)."""
    return f"{n:,}".replace(",", " ")


def read_bin_header(path):
    """Разбирает заголовок GCDCT2 → (всего пар, [(имя категории, пар), ...])."""
    with open(path, "rb") as f:
        head = f.read(1 << 16)
    if head[:6] != MAGIC:
        raise ValueError(f"не похоже на словарь GlyphCore: ожидалась сигнатура "
                         f"{MAGIC.decode()}, а там {head[:6]!r}")
    p = 6
    _flags, = struct.unpack_from("<H", head, p); p += 2
    total,  = struct.unpack_from("<I", head, p); p += 4
    ncat,   = struct.unpack_from("<H", head, p); p += 2
    cats = []
    for _ in range(ncat):
        ln = head[p]; p += 1
        name = head[p:p + ln].decode("utf-8", "replace"); p += ln
        cnt, = struct.unpack_from("<I", head, p); p += 4
        p += 8  # offset
        cats.append((name, cnt))
    return total, cats


def csv_stats():
    """По каждому рабочему CSV: строк и УНИКАЛЬНЫХ переведённых ключей.

    Считать надо именно уникальные: в main_strings.csv одна и та же английская
    строка встречается по многу раз (7,5 тыс. дублей), а bin хранит ключ один раз —
    иначе сравнение с bin всегда показывает мнимую пропажу.
    """
    files = sorted(glob.glob(os.path.join(GCDIR, "dict_*.csv")))
    ms = os.path.join(GCDIR, "main_strings.csv")
    if os.path.exists(ms):
        files.append(ms)
    stats = {}
    for f in files:
        keys = set()
        rows = 0
        with open(f, encoding="utf-8", newline="") as fh:
            r = csv.reader(fh)
            next(r, None)
            for row in r:
                if not row:
                    continue
                rows += 1
                if len(row) > 1 and row[1].strip():
                    keys.add(row[0])
        stats[os.path.basename(f)] = (rows, len(keys))
    return stats


def cat_to_file(name):
    """Имя категории в bin → имя рабочего CSV."""
    return "main_strings.csv" if name == "основной" else f"dict_{name}.csv"


def check_bin():
    """Проверяет bin перед упаковкой. Возвращает список предупреждений."""
    warn = []
    total, cats = read_bin_header(BIN)
    size = os.path.getsize(BIN)
    bin_mtime = os.path.getmtime(BIN)
    print(f"dictionary.bin: {size / 1_048_576:.1f} МБ | категорий: {len(cats)} | пар: {num(total)}")
    print(f"  собран: {datetime.datetime.fromtimestamp(bin_mtime):%Y-%m-%d %H:%M}")

    stats = csv_stats()
    if stats:
        rows = sum(v[0] for v in stats.values())
        uniq = sum(v[1] for v in stats.values())
        print(f"  рабочие CSV: {len(stats)} файл(ов), {num(rows)} строк, "
              f"{num(uniq)} уникальных переводов")

        # посравнимо: категория bin против одноимённого CSV
        behind, seen = [], set()
        for name, cnt in cats:
            fn = cat_to_file(name)
            seen.add(fn)
            if fn in stats and stats[fn][1] > cnt:
                behind.append((fn, stats[fn][1] - cnt))
        missing = [fn for fn in stats if fn not in seen and stats[fn][1] > 0]
        if behind:
            behind.sort(key=lambda x: -x[1])
            top = ", ".join(f"{fn} (+{num(d)})" for fn, d in behind[:3])
            warn.append(f"в {len(behind)} CSV переводов больше, чем в bin: {top}. "
                        f"Эти правки в релиз НЕ попадут — пересобери: оверлей → "
                        f"«Файлы перевода» → «CSV в bin».")
        if missing:
            warn.append(f"в bin нет категорий для {len(missing)} CSV "
                        f"({', '.join(sorted(missing)[:3])}) — их переводы в релиз не войдут.")
        if not behind and not missing:
            print("  сверка по категориям: bin не отстаёт ни от одного CSV")
    else:
        print("  рабочих CSV рядом нет — сверить свежесть bin не с чем")

    if any(c[0].startswith("pn_") or c[0] in ("имена", "proper_nouns") for c in cats):
        warn.append("в bin появились категории имён собственных — возможно, pn_*.csv "
                    "теперь входят в bin и класть их в архив отдельно больше не нужно.")
    return warn


def files_to_pack(mode):
    if mode == "bin":
        fs = [BIN, os.path.join(GCDIR, "config.ini")]
    else:
        fs = [os.path.join(GCDIR, "main_strings.csv"),
              os.path.join(GCDIR, "config.ini")]
        fs += sorted(glob.glob(os.path.join(GCDIR, "dict_*.csv")))
    fs += sorted(glob.glob(os.path.join(GCDIR, "pn_*.csv")))
    return [f for f in fs if os.path.exists(f)]


def main():
    mode = "csv" if "--csv" in sys.argv else "bin"
    ver = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if not ver:
        ver = "v" + datetime.date.today().strftime("%Y.%m.%d")

    warn = []
    if mode == "bin":
        if not os.path.exists(BIN):
            print("dictionary.bin не найден рядом с crowdsource/.")
            print("Собери его в игре: оверлей → «Файлы перевода» → «CSV в bin».")
            print("Либо собери старый формат: python make_release.py --csv")
            return 1
        try:
            warn = check_bin()
        except ValueError as e:
            print(f"dictionary.bin битый: {e}")
            return 1

    fs = files_to_pack(mode)
    pn = [f for f in fs if os.path.basename(f).startswith("pn_")]
    if not pn:
        warn.append("pn_*.csv не найдены — без них имена и локации останутся английскими.")

    zip_path = os.path.join(OUTDIR, f"gw2-ru-{ver}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in fs:
            z.write(f, os.path.basename(f))
        z.writestr("INSTALL.txt", INSTALL_BIN if mode == "bin" else INSTALL_CSV)

    size = os.path.getsize(zip_path)
    sha = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()
    print(f"\nСобран архив ({mode}): {zip_path}")
    print(f"  файлов: {len(fs) + 1} | размер: {size / 1_048_576:.1f} МБ")
    print(f"  sha256: {sha}")

    if warn:
        print("\nВНИМАНИЕ:")
        for w in warn:
            print(f"  ! {w}")

    print("\nОпубликовать релиз (crowdsource — public repo):")
    print("  1) git push origin main")
    print("  2a) через gh CLI:")
    print(f'      gh release create {ver} "{zip_path}" -t "Русификатор GW2 {ver}" -n "Локализация на {ver}."')
    print("  2b) без gh — через сайт:")
    print("      открой https://github.com/K13or/crowdsource/releases/new")
    print(f"      тег {ver}, перетащи в ассеты: {zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
