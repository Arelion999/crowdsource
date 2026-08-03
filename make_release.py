#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Готовит релиз русификатора: пересобирает glyphCore/dictionary.bin из ТЕКУЩИХ CSV,
проверяет его и печатает команду публикации. Архив не собирается — ассет релиза
один: сам dictionary.bin. Инструкция по установке живёт в README.md.

    python make_release.py                 # версия = дата (v2026.08.02)
    python make_release.py v2026.08.02     # свой тег
    python make_release.py --no-build      # не пересобирать bin, только проверить
    python make_release.py --no-lint       # не гонять линтер по игровым словарям
    python make_release.py --lint-details  # показать примеры найденных линтом ошибок

Сборка bin — тем же кодом, что кнопка «CSV в bin» в оверлее (csv_to_bin.py):
dict_*.csv + pn_*.csv + main_strings.csv («основной») + discovered_strings.csv
(«выученные»). Записи «только по хешу» из прежнего bin переносятся — см. csv_to_bin.
"""
import os, sys, csv, glob, struct, datetime, hashlib, collections

HERE  = os.path.dirname(os.path.abspath(__file__))        # crowdsource
GCDIR = os.path.dirname(HERE)                             # glyphCore
BIN   = os.path.join(GCDIR, "dictionary.bin")
MAGIC = b"GCDCT2"

sys.path.insert(0, HERE)
import csv_to_bin                                          # noqa: E402
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


def build_bin(carry):
    print("Сборка dictionary.bin из текущих CSV…")
    try:
        cats, total, per_file, carried = csv_to_bin.build(GCDIR, BIN, carry=carry)
    except PermissionError:
        print(f"[!] {BIN} занят — закрой игру/прокси и повтори.")
        return None
    if not cats:
        print(f"[!] В {GCDIR} не нашлось ни одной переведённой строки.")
        return None
    print(f"  категорий: {len(cats)} | пар: {num(total)}")
    if carried:
        print(f"  перенесено выученного из прежнего bin: {num(carried)} "
              f"(этих строк нет ни в одном CSV)")
    return per_file


def check_bin(per_file):
    """Сверяет bin с CSV. Возвращает список предупреждений."""
    warn = []
    total, cats = read_header(BIN)
    print(f"\ndictionary.bin: {os.path.getsize(BIN) / 1_048_576:.1f} МБ | "
          f"категорий: {len(cats)} | пар: {num(total)}")
    print(f"  собран: {datetime.datetime.fromtimestamp(os.path.getmtime(BIN)):%Y-%m-%d %H:%M}")

    if per_file is None:                                   # запускались с --no-build
        per_file = [(f, c, len(csv_to_bin.load_rows(csv_to_bin.read_text(os.path.join(GCDIR, f)))))
                    for f, c in csv_to_bin.sources(GCDIR)]

    have = {n.split("\x1f")[0] for n, _ in cats}
    missing = [f for f, cat, n in per_file if n and cat not in have]
    if missing:
        warn.append(f"в bin нет категорий для {len(missing)} CSV "
                    f"({', '.join(sorted(missing)[:3])}) — их переводы в релиз не войдут.")
    rows = sum(n for _, _, n in per_file)
    print(f"  источники: {len(per_file)} CSV, {num(rows)} переведённых строк "
          f"(в bin меньше — дубли сворачиваются по хешу)")

    if not any(f.startswith("pn_") for f, _, n in per_file if n):
        warn.append("pn_*.csv не найдены — имена и локации останутся английскими.")
    return warn


def lint(details):
    """Линтер по игровым словарям. Ошибки не блокируют релиз (они уже в игре),
    но их видно до публикации. Возвращает список предупреждений."""
    files = sorted(glob.glob(os.path.join(GCDIR, "dict_*.csv")))
    ms = os.path.join(GCDIR, "main_strings.csv")
    if os.path.exists(ms):
        files.append(ms)
    kinds, examples, checked, bad = collections.Counter(), {}, 0, 0
    for fp in files:
        with open(fp, encoding="utf-8", newline="") as fh:
            for i, r in enumerate(csv.reader(fh), start=1):
                if len(r) < 2 or not r[1].strip() or r[0].lower() == "english":
                    continue
                checked += 1
                errs, _ = validate.check_row(r[0], r[1])
                if errs:
                    bad += 1
                for m in errs:
                    k = m.split(":")[0].split("«")[0].strip()
                    kinds[k] += 1
                    examples.setdefault(k, (os.path.basename(fp), i, r[0][:70], r[1][:70]))
    print(f"\nЛинт игровых словарей: проверено {num(checked)} строк, "
          f"битых {num(bad)}")
    for k, c in kinds.most_common():
        print(f"  {num(c):>7}  {k}")
        if details:
            f, i, en, ru = examples[k]
            print(f"           {f}:{i}\n             EN {en!r}\n             RU {ru!r}")
    if not details and kinds:
        print("  (примеры: python make_release.py --lint-details)")
    return ([f"линт нашёл {num(bad)} битых строк в игровых словарях — "
             f"они попадут в релиз как есть."] if bad else [])


def main():
    ver = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if not ver:
        ver = "v" + datetime.date.today().strftime("%Y.%m.%d")

    per_file = None
    if "--no-build" not in sys.argv:
        carry = None if "--no-carry" in sys.argv else (BIN if os.path.exists(BIN) else None)
        per_file = build_bin(carry)
        if per_file is None:
            return 1
    elif not os.path.exists(BIN):
        print(f"[!] {BIN} не найден, а сборка отключена (--no-build).")
        return 1

    try:
        warn = check_bin(per_file)
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
