#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Собирает dictionary.bin (формат GCDCT2) из ТЕКУЩИХ CSV папки glyphCore/ — ровно то же,
что делает оверлей кнопкой «CSV в bin» (import_csvs_to_bin в dict_bin.rs).

    python csv_to_bin.py                     # папка glyphCore (родитель crowdsource/) -> её dictionary.bin
    python csv_to_bin.py <csv_dir>           # своя папка, bin рядом с ней
    python csv_to_bin.py <csv_dir> <out.bin>
    python csv_to_bin.py --no-carry          # не переносить выученное из старого bin (см. ниже)
    python csv_to_bin.py --keep-empty        # класть в bin и непереведённые ключи (+13 МБ, обычно не надо)

Что попадает в bin и под каким именем категории:
    dict_<name>.csv          -> <name>          (если в 1-й строке 3-я колонка непустая
                                                 и не равна <name> — «<name>\x1f<display>»)
    pn_*.csv                 -> pn_*            (слой имён собственных)
    main_strings.csv         -> основной        (cyrillic_strings.csv — старое имя того же)
    discovered_strings.csv   -> выученные       (строки, собранные прокси на лету)

Правила разбора строк (как в моде):
  * col0 = english, col1 = русский (пустой перевод — строка пропускается, см. --keep-empty);
    игнорируется только строка-заголовок (col0 == "english"). Строки на '#' НЕ комментарии:
    в игре есть лорные книги, которые так и начинаются («### Когда вы сжимаете топорище…»),
    и их переводы (66 штук) раньше терялись;
  * hash = FNV-1a-64 по UTF-16LE английской строки; голые 16 hex-символов в col0 —
    запись «только по хешу» (hex И ЕСТЬ хеш, english хранится пустым), чтобы
    round-trip bin -> csv -> bin ничего не терял;
  * внутри категории записи сортируются по хешу и дедуплицируются (побеждает первая).

Порядок категорий в файле (dict_* по алфавиту, затем pn_*, затем основной и выученные)
совпадает с оверлеем — пересборка без правок в CSV даёт байт-в-байт тот же bin.

ПЕРЕНОС ВЫУЧЕННОГО (--no-carry отключает). В категории «выученные» лежат записи
«только по хешу»: английского текста у них нет, и в discovered_strings.csv их переводов
тоже нет — они существуют ТОЛЬКО внутри bin. Поэтому перед записью старый bin читается,
и все его записи с пустым english, которых нет в свежей сборке, переносятся в новый файл.
Без этого пересборка из CSV молча теряет ~1000 переводов выученных строк.
"""
import os, struct, sys

FNV_OFFSET = 0xcbf29ce484222325
FNV_PRIME  = 0x100000001b3
MASK64 = (1 << 64) - 1

# main_strings.csv и cyrillic_strings.csv — одно и то же (файл переименовали), обе
# складываются в категорию «основной».
PRIMARY   = (("main_strings.csv", "основной"),
             ("cyrillic_strings.csv", "основной"),
             ("discovered_strings.csv", "выученные"))


def fnv1a_u16(s):
    """FNV-1a-64 по код-юнитам UTF-16LE (младший байт, затем старший)."""
    h = FNV_OFFSET
    for ch in s.encode("utf-16-le"):
        h ^= ch; h = (h * FNV_PRIME) & MASK64
    return h


def parse_csv(text):
    r"""RFC-4180-ish разбор, как parse_csv в моде: кавычки с "" внутри, поля через ',',
    '\r' игнорируется, '\n' завершает запись."""
    rows, rec_fields, field_chars, q = [], [], [], False
    k, n = 0, len(text)
    while k < n:
        c = text[k]
        if q:
            if c == '"':
                if k + 1 < n and text[k + 1] == '"':
                    field_chars.append('"'); k += 1
                else:
                    q = False
            else:
                field_chars.append(c)
        else:
            if c == '"':
                q = True
            elif c == ',':
                rec_fields.append("".join(field_chars)); field_chars = []
            elif c == '\r':
                pass
            elif c == '\n':
                rec_fields.append("".join(field_chars)); field_chars = []
                rows.append(rec_fields); rec_fields = []
            else:
                field_chars.append(c)
        k += 1
    if field_chars or rec_fields:
        rec_fields.append("".join(field_chars)); rows.append(rec_fields)
    return rows


def row_hash_en(en):
    """(hash, что хранить в english): голые 16 hex -> запись только по хешу."""
    if len(en) == 16 and all(c in "0123456789abcdefABCDEF" for c in en):
        try:
            return (int(en, 16), "")
        except ValueError:
            pass
    return (fnv1a_u16(en), en)


def load_rows(text, keep_empty=False):
    out = []
    for rec in parse_csv(text):
        if len(rec) < 2:
            continue
        en = rec[0].strip()
        if not en or en.lower() == "english":
            continue
        if not rec[1].strip() and not keep_empty:
            continue
        h, en_stored = row_hash_en(en)
        if len(en_stored.encode("utf-16-le")) > 0xffff * 2:
            continue
        if len(rec[1].encode("utf-16-le")) > 0xffff * 2:
            continue
        out.append((h, en_stored, rec[1]))
    return out


def read_text(path):
    with open(path, "rb") as f:
        b = f.read()
    if b[:3] == b"\xef\xbb\xbf":
        b = b[3:]
    return b.decode("utf-8", "replace")


def read_bin(path):
    """Читает готовый bin -> [(имя категории, [(hash, english, русский), ...]), ...]."""
    with open(path, "rb") as f:
        b = f.read()
    if b[:6] != b"GCDCT2":
        raise ValueError(f"не словарь GlyphCore: сигнатура {b[:6]!r}, ожидалась GCDCT2")
    p = 8
    _total, = struct.unpack_from("<I", b, p); p += 4
    ncat,   = struct.unpack_from("<H", b, p); p += 2
    heads = []
    for _ in range(ncat):
        ln = b[p]; p += 1
        name = b[p:p + ln].decode("utf-8", "replace"); p += ln
        cnt,  = struct.unpack_from("<I", b, p); p += 4
        off,  = struct.unpack_from("<Q", b, p); p += 8
        heads.append((name, cnt, off))
    out = []
    for name, cnt, off in heads:
        q, es = off, []
        for _ in range(cnt):
            q += 4                                    # id записи — пересчитывается при сборке
            h, = struct.unpack_from("<Q", b, q); q += 8
            l, = struct.unpack_from("<H", b, q); q += 2
            en = b[q:q + l * 2].decode("utf-16-le", "replace"); q += l * 2
            l, = struct.unpack_from("<H", b, q); q += 2
            ru = b[q:q + l * 2].decode("utf-16-le", "replace"); q += l * 2
            es.append((h, en, ru))
        out.append((name, es))
    return out


def sources(csv_dir):
    """Список (имя файла, имя категории) в том же порядке, в каком их кладёт оверлей."""
    names = set(os.listdir(csv_dir))
    dicts = sorted(n for n in names
                   if n.lower().startswith("dict_") and n.lower().endswith(".csv"))
    pns = sorted(n for n in names
                 if n.lower().startswith("pn_") and n.lower().endswith(".csv"))
    out = [(n, n[5:-4]) for n in dicts]          # dict_items.csv -> items
    out += [(n, n[:-4]) for n in pns]            # pn_names.csv   -> pn_names
    out += [(n, cat) for n, cat in PRIMARY if n in names]
    return out


def build(csv_dir, out_path, carry=None, keep_empty=False):
    """Собирает bin из CSV папки csv_dir.

    carry — путь к старому bin, из которого переносится «выученное» (записи с пустым
    english, которых нет в свежей сборке); None — не переносить.

    keep_empty — класть в bin и НЕпереведённые ключи. Оверлей так не делает: пустых
    переводов набирается 167 тыс. (+13 МБ к релизу), а пользы от них в игре нет.

    Возвращает (категории [(имя, записей)], всего записей,
    по файлам [(файл, категория, переведённых строк)], перенесено записей)."""
    sections, per_file = [], []
    by_cat = {}
    for fname, cat in sources(csv_dir):
        text = read_text(os.path.join(csv_dir, fname))
        entries = load_rows(text, keep_empty)
        per_file.append((fname, cat, len(entries)))
        if not entries:
            continue
        if fname.lower().startswith("dict_"):
            recs = parse_csv(text)
            if recs and len(recs[0]) >= 3:
                dsp = recs[0][2].strip()
                if dsp and dsp != cat:
                    cat = f"{cat}\x1f{dsp}"
        if cat in by_cat:                        # основной = main_strings + cyrillic_strings
            by_cat[cat].extend(entries)
        else:
            by_cat[cat] = entries
            sections.append(cat)

    # перенос выученного: записи «только по хешу» из старого bin, которых нет в CSV
    carried = 0
    if carry and os.path.exists(carry):
        fresh = {h for es in by_cat.values() for h, _, _ in es}
        for cat, es in read_bin(carry):
            keep = [e for e in es if not e[1] and e[0] not in fresh]
            if not keep:
                continue
            if cat not in by_cat:
                by_cat[cat] = []
                sections.append(cat)
            by_cat[cat].extend(keep)
            carried += len(keep)

    # нормализация: сортировка по хешу + дедуп (побеждает первая запись)
    norm = []
    for cat in sections:
        es = sorted(by_cat[cat], key=lambda e: e[0])
        seen, ded = set(), []
        for e in es:
            if e[0] in seen:
                continue
            seen.add(e[0]); ded.append(e)
        if ded:
            norm.append((cat, ded))
    if not norm:
        return [], 0, per_file, carried

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
    out += b"GCDCT2\x00\x00"
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

    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, out_path)
    return [(n, len(es)) for n, es in norm], total, per_file, carried


def default_dir():
    """Папка glyphCore — родитель crowdsource/, где лежит этот скрипт."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    csv_dir  = args[0] if args else default_dir()
    out_path = args[1] if len(args) > 1 else os.path.join(csv_dir, "dictionary.bin")
    if not os.path.isdir(csv_dir):
        print(f"[!] Папка не найдена: {csv_dir}")
        return 1
    if os.path.isdir(out_path) or out_path.endswith(("\\", "/")):
        out_path = os.path.join(out_path, "dictionary.bin")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    carry = None if "--no-carry" in sys.argv else (out_path if os.path.exists(out_path) else None)
    try:
        cats, total, per_file, carried = build(csv_dir, out_path, carry,
                                               keep_empty="--keep-empty" in sys.argv)
    except PermissionError:
        print(f"[!] Не могу записать {out_path} — файл занят. Закрой игру/прокси и повтори.")
        return 1
    if not cats:
        print(f"[!] В {csv_dir} не нашлось ни одной переведённой строки.")
        return 1
    if carried:
        print(f"[i] Перенесено из старого bin (выученное, в CSV его нет): {carried}")
    print(f"[+] Готово: {len(cats)} категорий, {total} записей, "
          f"{os.path.getsize(out_path) / 1_048_576:.1f} МБ")
    print(f"    -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
