#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовит эталон для сверки линтера: server_ref.py <дамп с /list>

Дамп отдаёт 218 951 строку вида `хеш,english` — 13 МБ. Линтеру из них нужны
только те, что несут плейсхолдер или плюрал-разметку: лишь по ним видно, что до
нас доехала испорченная копия оригинала. Это 35 539 строк и 1.2 МБ.
"""
import csv, io, os, re, sys

csv.field_size_limit(10**8)
MARK = re.compile(r'%\w+%|\[s\]|\[pl:"|\[nosep\]|\[null\]')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "sync", "server_strings.csv")

src = sys.argv[1] if len(sys.argv) > 1 else None
if not src:
    sys.exit("нужен путь к дампу: server_ref.py <list.csv>")

rows = {r[1] for r in csv.reader(io.StringIO(open(src, "rb").read().decode("utf-8", errors="replace"),
                                             newline=""))
        if len(r) > 1 and r[1] and MARK.search(r[1])}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
buf = io.StringIO(newline="")
w = csv.writer(buf, lineterminator="\n")
w.writerow(["english"])
for s in sorted(rows):
    w.writerow([s])
open(OUT, "wb").write(buf.getvalue().encode("utf-8"))
print("записано %d строк -> %s" % (len(rows), OUT))
