#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_tool.py — рабочий инструмент для батчей краудсорс-перевода GW2 RU.

Заменяет одноразовые fill_NNN.py: один постоянный путь => одно правило разрешений.

Команды
-------
  list  [--type new] [--limit 15]      свободные батчи, ранжированные по плотности прозы
  dump  <батч> [--start N] [--end M]   английская колонка с индексами (для сверки выравнивания)
  fill  <батч> <переводы.json> [--dry-run] [--allow-blanks]
  claim <батч> [--who "..."] [--unclaim]

Формат <переводы.json> (UTF-8), одно из двух:
  1) список из N строк — позиционно, ровно по числу строк данных в батче;
  2) объект {english: russian} — по ключу; недостающие ключи => пустой перевод.

Почему JSON, а не текст по строкам: в 2325 строках батчей есть настоящие переносы
внутри ячеек (письма в items_*), поэтому «одна строка = один перевод» ломается.

Проверки ДО записи (при провале файл не трогается, exit 1):
  * совпадение количества строк / наличие ключей;
  * мультимножество токенов %strN%/%numN%/<...>/[lbracket]/[rbracket]/[null] построчно;
  * число %% построчно;
  * отсутствие литералов [s] и [pl:"..."] в переводе (их обязано заменить склонение);
  * отсутствие U+FFFD (битая кодировка).
После записи: сверка колонки english с бэкапом байт-в-байт + прогон validate.py.
"""
import argparse, csv, glob, io, json, os, re, shutil, subprocess, sys, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CROWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # ...\crowdsource
ROOT = os.path.dirname(CROWD)                                        # ...\glyphCore
CLAIMS = os.path.join(CROWD, "CLAIMS.md")
VALIDATE = os.path.join(CROWD, "validate.py")
BAKDIR = os.path.join(CROWD, ".batch_bak")
DEFAULT_WHO = "Магистр Клод, Приорат Дурманд"

TOK = re.compile(r'%\w+%|<[^>]+>|\[lbracket\]|\[rbracket\]|\[null\]')
PLACEHOLDER = re.compile(r'%\w+%')
LEFTOVER = re.compile(r'\[s\]|\[pl:')
# CJK/кана/хангыль в русском переводе — всегда опечатка (проскакивает при наборе).
# validate.py такое не ловит: это не токен и не битая кодировка.
CJK = re.compile(r'[぀-ヿ㐀-䶿一-鿿가-힯]')
# Служебные токены вырезаем перед поиском латиницы, иначе [lbracket] даёт ложное срабатывание.
SERVICE = re.compile(r'%\w+%|<[^>]+>|\[lbracket\]|\[rbracket\]|\[null\]|\[pl:"[^"]*"\]')
LATIN = re.compile(r'[A-Za-z]')


def toks(s):
    return sorted(TOK.findall(s))


def lint_row(en, ru):
    """Ошибки validate.py для одной строки (пусто, если линтер недоступен).

    Импортируем сам линтер, а не копируем правила: одна реализация — один канон.
    """
    global _VALIDATE_MOD
    try:
        if _VALIDATE_MOD is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location("validate_lint", VALIDATE)
            _VALIDATE_MOD = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_VALIDATE_MOD)
    except Exception as e:                      # линтер не найден/сломан — не роняем fill
        print(f"⚠ не удалось подключить validate.py ({e}); построчный линт пропущен")
        _VALIDATE_MOD = False
    if not _VALIDATE_MOD:
        return []
    return _VALIDATE_MOD.check_row(en, ru)[0]


_VALIDATE_MOD = None


def pct_residue(s):
    """Число экранированных '%' вне плейсхолдеров.

    Точнее, чем count('%%'): та не отличает '%num1%%%' (число + знак %)
    от битого '%num1%%' — обе дают 1. Здесь: 2 против 1.
    """
    return PLACEHOLDER.sub("", s).count("%")


def resolve(batch):
    """Принимает 'new_016', 'new_016.csv', 'new/new_016.csv' или полный путь."""
    if os.path.isfile(batch):
        return os.path.abspath(batch)
    cand = batch if batch.endswith(".csv") else batch + ".csv"
    p = os.path.join(CROWD, cand)
    if os.path.isfile(p):
        return p
    hits = glob.glob(os.path.join(CROWD, "*", os.path.basename(cand)))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit(f"батч не найден: {batch}")
    sys.exit("неоднозначно: " + ", ".join(hits))


def read_batch(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        sys.exit(f"пустой файл: {path}")
    return rows[0], [r for r in rows[1:] if r]


def write_batch(path, header, pairs):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for en, ru in pairs:
            w.writerow([en, ru])


# ---------------------------------------------------------------- list
def claimed_set():
    if not os.path.isfile(CLAIMS):
        return set()
    txt = open(CLAIMS, encoding="utf-8").read()
    return set(re.findall(r'`([^`]+\.csv)`[^\n]*\|\s*(?:✅|🔨)', txt))


def cmd_list(a):
    done = claimed_set()
    rows = []
    pat = os.path.join(CROWD, (a.type or "*"), "*.csv")
    for fp in sorted(glob.glob(pat)):
        base = os.path.basename(fp)
        if base in done:
            continue
        _, data = read_batch(fp)
        engs = [r[0] for r in data]
        todo = [r for r in data if len(r) < 2 or not r[1].strip()]
        if not engs or not todo:
            continue
        avg = sum(len(e) for e in engs) / len(engs)
        prose = sum(1 for e in engs
                    if e[:1] in '"\'' or (' ' in e and len(e) > 25 and re.search(r'[a-z].*[.!?…]$', e)))
        rows.append((base, len(data), len(todo), round(avg, 1), round(prose / len(engs) * 100)))
    rows.sort(key=lambda r: -r[3])
    print(f"{'батч':22}{'строк':>7}{'пусто':>7}{'ср.длина':>10}{'проза%':>8}")
    for r in rows[:a.limit]:
        print(f"{r[0]:22}{r[1]:7}{r[2]:7}{r[3]:10}{r[4]:8}")
    if not rows:
        print("(свободных батчей нет)")


# ---------------------------------------------------------------- dump
def cmd_dump(a):
    path = resolve(a.batch)
    _, data = read_batch(path)
    lo = a.start if a.start is not None else 0
    hi = a.end if a.end is not None else len(data)
    for i in range(lo, min(hi, len(data))):
        mark = "" if (len(data[i]) > 1 and data[i][1].strip()) else " *"
        print(f"[{i}] L{i+2}{mark}\t{data[i][0]}")
    print(f"-- показано {min(hi,len(data))-lo} из {len(data)} строк данных (* = не переведено)")


# ---------------------------------------------------------------- fill
def cmd_fill(a):
    path = resolve(a.batch)
    header, data = read_batch(path)
    engs = [r[0] for r in data]

    with open(a.translations, encoding="utf-8-sig") as f:
        payload = json.load(f)

    if isinstance(payload, list):
        if len(payload) != len(data):
            sys.exit(f"ABORT: в батче {len(data)} строк данных, в JSON-списке {len(payload)}")
        new = [("" if v is None else str(v)) for v in payload]
        mode = "позиционный"
    elif isinstance(payload, dict):
        new = [str(payload.get(e, "")) for e in engs]
        missing = sum(1 for e, v in zip(engs, new) if not v.strip() and e.strip())
        mode = f"по ключу (без перевода: {missing})"
    else:
        sys.exit("ABORT: JSON должен быть списком строк или объектом {english: russian}")

    # ---- гейты до записи
    errs = []
    for i, (en, ru) in enumerate(zip(engs, new)):
        ln = i + 2
        if not ru.strip():
            continue
        if toks(en) != toks(ru):
            errs.append(f"  L{ln} [{i}] токены не совпадают\n      EN {toks(en)}\n      RU {toks(ru)}\n      «{en[:70]}»")
        if pct_residue(en) != pct_residue(ru):
            errs.append(f"  L{ln} [{i}] знаков % вне плейсхолдеров {pct_residue(en)} != {pct_residue(ru)}  «{en[:60]}»")
        if LEFTOVER.search(ru):
            errs.append(f"  L{ln} [{i}] в переводе остался [s] / [pl:  «{ru[:70]}»")
        if "�" in ru:
            errs.append(f"  L{ln} [{i}] U+FFFD (битая кодировка)")
        m = CJK.search(ru)
        if m:
            errs.append(f"  L{ln} [{i}] посторонний символ {m.group()!r} (CJK/кана) — опечатка  «{ru[:60]}»")
        # Полный набор проверок линтера — тот же, что блокирует релиз. Гоняем ДО записи,
        # чтобы «потерян префикс Recipe[s]:» или двухформенный плюрал не попали в файл.
        for msg in lint_row(en, ru):
            errs.append(f"  L{ln} [{i}] {msg}  «{en[:55]}» -> «{ru[:55]}»")

    # ПРЕДУПРЕЖДЕНИЕ (не блокирует): латиница в переводе — обычно строка, забытая
    # непереведённой. Иногда законна (названия дополнений), поэтому только сигнал.
    latin = [(i + 2, en, ru) for i, (en, ru) in enumerate(zip(engs, new))
             if ru.strip() and LATIN.search(SERVICE.sub("", ru))]

    blanks = sum(1 for en, ru in zip(engs, new) if en.strip() and not ru.strip())
    if blanks and not a.allow_blanks:
        errs.append(f"  пустых переводов: {blanks} (осознанно? добавьте --allow-blanks)")

    if errs:
        print(f"ABORT: {len(errs)} проблем(ы), файл не изменён:")
        print("\n".join(errs[:40]))
        if len(errs) > 40:
            print(f"  … и ещё {len(errs)-40}")
        return 1

    print(f"гейты пройдены | режим: {mode} | строк: {len(data)} | заполнено: {len(data)-blanks} | пусто: {blanks}")
    if latin:
        print(f"  ⚠ латиница в переводе — {len(latin)} строк(и), проверьте, не забыт ли перевод:")
        for ln, en, ru in latin[:12]:
            print(f"      L{ln}  «{en[:40]}» -> «{ru[:60]}»")
        if len(latin) > 12:
            print(f"      … и ещё {len(latin)-12}")
    if a.dry_run:
        print("--dry-run: запись не выполнена")
        return 0

    # ---- бэкап + запись
    os.makedirs(BAKDIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(BAKDIR, f"{os.path.basename(path)}.{stamp}.bak")
    shutil.copy2(path, bak)
    write_batch(path, header, list(zip(engs, new)))

    # ---- постпроверка: english обязан совпасть с бэкапом
    _, before = read_batch(bak)
    _, after = read_batch(path)
    diffs = [i for i, (x, y) in enumerate(zip(before, after)) if x[0] != y[0]]
    if len(before) != len(after) or diffs:
        shutil.copy2(bak, path)
        sys.exit(f"ABORT: колонка english изменилась ({len(diffs)} строк) — откат из {bak}")
    print(f"english не изменён (сверено {len(after)} строк) | бэкап: {os.path.relpath(bak, ROOT)}")

    # ---- validate.py
    r = subprocess.run([sys.executable, VALIDATE, "--warnings", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=CROWD)
    print((r.stdout or "").strip() or "(validate.py без вывода)")
    if r.returncode != 0:
        print((r.stderr or "").strip())
        print("ВНИМАНИЕ: validate.py вернул ошибки — откатить можно из бэкапа выше")
        return 1
    return 0


# ---------------------------------------------------------------- claim
def cmd_claim(a):
    base = os.path.basename(resolve(a.batch))
    lines = open(CLAIMS, encoding="utf-8").read().split("\n")
    hit = None
    for i, ln in enumerate(lines):
        if ln.startswith("|") and f"`{base}`" in ln:
            hit = i
            break
    if hit is None:
        sys.exit(f"строка для {base} не найдена в CLAIMS.md")
    parts = lines[hit].split("|")
    if len(parts) < 4:
        sys.exit(f"неожиданный формат строки: {lines[hit][:80]}")
    parts[-3] = "  " if a.unclaim else " ✅ "
    parts[-2] = "  " if a.unclaim else f" {a.who} "
    lines[hit] = "|".join(parts)
    open(CLAIMS, "w", encoding="utf-8", newline="").write("\n".join(lines))
    print(("снята отметка: " if a.unclaim else "отмечено ✅: ") + base)
    print("  " + lines[hit].strip()[:150])


def main():
    ap = argparse.ArgumentParser(description="Инструмент батчей перевода GW2 RU")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="свободные батчи по плотности прозы")
    p.add_argument("--type", default=None, help="подпапка: new, ui, items…")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("dump", help="english с индексами")
    p.add_argument("batch")
    p.add_argument("--start", type=int)
    p.add_argument("--end", type=int)
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("fill", help="заполнить переводы с проверками")
    p.add_argument("batch")
    p.add_argument("translations", help="JSON: список строк или {english: russian}")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-blanks", action="store_true", help="разрешить пустые переводы")
    p.set_defaults(fn=cmd_fill)

    p = sub.add_parser("claim", help="отметить батч в CLAIMS.md")
    p.add_argument("batch")
    p.add_argument("--who", default=DEFAULT_WHO)
    p.add_argument("--unclaim", action="store_true")
    p.set_defaults(fn=cmd_claim)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
