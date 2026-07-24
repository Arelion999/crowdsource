#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отчёт о покрытии перевода.

    python stats.py                # сводка по батчам (категории, проценты) + покрытие cyrillic_strings.csv
    python stats.py --batches      # ещё и построчный список незавершённых батчей
    python stats.py --mark-done    # проставить ✅ в CLAIMS.md батчам, переведённым на 100%% (статус, ник не трогает)
"""
import csv, glob, os, re, sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE  = os.path.dirname(os.path.abspath(__file__))
CYR   = os.path.join(os.path.dirname(HERE), "cyrillic_strings.csv")
CLAIMS = os.path.join(HERE, "CLAIMS.md")

def batch_stats():
    per_cat = {}   # cat -> [rows, translated, nbatches]
    per_batch = {} # name -> (rows, translated)
    for fp in sorted(glob.glob(os.path.join(HERE, "*", "*.csv"))):
        name = os.path.basename(fp)
        cat = name.split("_")[0]
        try:
            data = list(csv.reader(open(fp, encoding="utf-8")))[1:]
        except Exception:
            continue
        rows = len(data)
        tr = sum(1 for r in data if len(r) > 1 and r[1].strip())
        per_batch[name] = (rows, tr)
        c = per_cat.setdefault(cat, [0, 0, 0])
        c[0] += rows; c[1] += tr; c[2] += 1
    return per_cat, per_batch

def cyrillic_coverage():
    seen = {}
    try:
        with open(CYR, encoding="utf-8") as f:
            r = csv.reader(f); next(r, None)
            for row in r:
                if not row:
                    continue
                en = row[0]
                tr = len(row) > 1 and bool(row[1].strip())
                # строка считается переведённой, если ХОТЯ БЫ одна её копия переведена
                seen[en] = seen.get(en, False) or tr
    except Exception:
        return None
    total = len(seen); done = sum(1 for v in seen.values() if v)
    return total, done

def pct(a, b):
    return f"{100*a/b:5.1f}%" if b else "   -  "

def main():
    per_cat, per_batch = batch_stats()
    print("=== Покрытие батчей по категориям ===")
    print(f"{'категория':12} {'батчей':>7} {'строк':>8} {'переведено':>11} {'%':>7}")
    tot_r = tot_t = tot_b = 0
    for cat in sorted(per_cat):
        rows, tr, nb = per_cat[cat]
        print(f"{cat:12} {nb:>7} {rows:>8,} {tr:>11,} {pct(tr,rows):>7}")
        tot_r += rows; tot_t += tr; tot_b += nb
    print(f"{'ИТОГО':12} {tot_b:>7} {tot_r:>8,} {tot_t:>11,} {pct(tot_t,tot_r):>7}")

    done = sum(1 for r, t in per_batch.values() if r and t == r)
    empty = sum(1 for r, t in per_batch.values() if t == 0)
    part = len(per_batch) - done - empty
    print(f"\nСтатус батчей: готовых(100%%)={done}, частичных={part}, пустых={empty}")

    cov = cyrillic_coverage()
    if cov:
        total, d = cov
        print(f"\nПокрытие cyrillic_strings.csv (уник. строк): {d:,} / {total:,} = {pct(d,total).strip()}")

    if "--batches" in sys.argv:
        print("\n=== Незавершённые батчи ===")
        for name in sorted(per_batch):
            rows, tr = per_batch[name]
            if rows and tr < rows:
                print(f"  {name:16} {tr:>4}/{rows:<4} {pct(tr,rows).strip()}")

    if "--mark-done" in sys.argv:
        mark_done(per_batch)

def mark_done(per_batch):
    if not os.path.exists(CLAIMS):
        print("CLAIMS.md не найден."); return
    lines = open(CLAIMS, encoding="utf-8").read().split("\n")
    changed = 0
    for i, line in enumerate(lines):
        m = re.match(r"\| `([^`]+\.csv)` \|", line)
        if not m:
            continue
        rt = per_batch.get(m.group(1))
        if not rt or not (rt[0] and rt[1] == rt[0]):
            continue
        parts = line.split("|")            # ['', ' `x` ', тип, строк, знаков, образец, статус, ник, '']
        if len(parts) < 9:
            continue
        if parts[-3].strip() == "✅":
            continue
        parts[-3] = " ✅ "
        lines[i] = "|".join(parts)
        changed += 1
    if changed:
        open(CLAIMS, "w", encoding="utf-8", newline="").write("\n".join(lines))
    print(f"\nCLAIMS.md: проставлено ✅ готовым батчам: {changed}")

if __name__ == "__main__":
    main()
