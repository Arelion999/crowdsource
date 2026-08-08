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

every = {r[1] for r in csv.reader(io.StringIO(open(src, "rb").read().decode("utf-8", errors="replace"),
                                              newline=""))
         if len(r) > 1 and r[1]}
rows = {s for s in every if MARK.search(s)}

# Второй колонкой — есть ли в игре та же строка БЕЗ числа, сама по себе.
# «Gold», «XP», «Tier: » существуют как подписи интерфейса рядом со счётчиками
# «%num1% Gold», «%num1% XP», «Tier: %num1%». Для таких пар совпадение «наша
# строка = серверная минус плейсхолдер» ничего не доказывает: наша копия скорее
# всего и есть подпись, а не обрезок. Проверка обязана их пропускать.
def bare(s):
    return re.sub(r'\s+', ' ', re.sub(r'%num\d*%', '', s)).strip()

os.makedirs(os.path.dirname(OUT), exist_ok=True)
buf = io.StringIO(newline="")
w = csv.writer(buf, lineterminator="\n")
# Первая колонка НЕ «english»: иначе линтер примет эталон за батч и
# начнёт проверять его сам с собой.
w.writerow(["source", "bare_exists"])
collide = 0
for s in sorted(rows):
    b = bare(s) in every and bare(s) != s
    collide += b
    w.writerow([s, "1" if b else ""])
open(OUT, "wb").write(buf.getvalue().encode("utf-8"))
print("записано %d строк (из них %d с голым двойником) -> %s" % (len(rows), collide, OUT))
