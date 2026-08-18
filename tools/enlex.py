# -*- coding: utf-8 -*-
"""Строит словарь английских слов из bin и делит батч на английский/французский.

Идея: bin — это 470 тысяч подлинных английских строк игры. Слово, которого там
нет ни разу, английским почти наверняка не является. Строка, у которой ни одно
слово длиной 4+ не встречается в этом словаре, — не английская.
"""
import csv, io, os, re, sys, pickle
os.chdir(r"C:/Games/Guild Wars 2/glyphCore/crowdsource")
sys.path.insert(0, "tools")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CACHE = os.path.join(os.environ["SCRATCH"], "enlex.pkl")

def build():
    import dict_tool as D
    lex = {}
    for head, entries in D.read_sections(r"C:/Games/Guild Wars 2/glyphCore/dictionary.bin"):
        for _h, en, _ru in entries:
            for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", en or ""):
                w = w.lower()
                lex[w] = lex.get(w, 0) + 1
    return lex

if os.path.exists(CACHE):
    lex = pickle.load(open(CACHE, "rb"))
else:
    lex = build()
    pickle.dump(lex, open(CACHE, "wb"))
print("слов в английском словаре bin:", len(lex), file=sys.stderr)

# слова, которые в bin встречаются считаные разы, доверия не заслуживают
def known(w):
    return lex.get(w.lower(), 0) >= 5

def classify(e):
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'-]{3,}", e)]
    if not words:
        return "neutral"
    hit = sum(1 for w in words if known(w))
    if hit == 0:
        return "fr"
    if hit < len(words) / 2.0:
        return "doubt"
    return "en"

if __name__ == "__main__":
    for b in sys.argv[1:]:
        rows = [r[0] for r in list(csv.reader(io.open("new/new_%s.csv" % b, encoding="utf-8")))[1:] if r and r[0].strip()]
        cnt = {"en": 0, "fr": 0, "doubt": 0, "neutral": 0}
        for e in rows: cnt[classify(e)] += 1
        print("new_%s: %s" % (b, cnt))
