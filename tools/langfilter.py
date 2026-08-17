#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Не-английские строки в батчах: найти, выписать, удалить.

    python tools/langfilter.py check [new/new_125.csv ...]   # список, без правок
    python tools/langfilter.py apply [new/new_125.csv ...]   # удалить уверенные
    python tools/langfilter.py apply --all [...]             # и «под вопросом» тоже

Зачем. Синк тянет строки с прокси, а прокси видел не только английский клиент:
в колонку `english` попадают французские (реже немецкие и испанские) строки.
Переводить их нельзя — в игре по этому хешу лежит французский оригинал, и наш
русский текст встал бы поверх французского клиента.

Признак не один, иначе ловится мусор: у «Place the Café Register decoration»
диакритика есть, а строка английская. Считаем ТРИ сигнала — служебные слова
языка, диакритику и характерные хвосты команд — и сравниваем с английскими
служебными словами. Отчёт всегда пишется целиком, удаляются только уверенные.
"""
import csv, glob, io, os, re, sys, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CROWD = os.path.dirname(HERE)
REPORT = os.path.join(CROWD, "sync", "reports", "non_english.csv")
REPORT_APPLIED = os.path.join(CROWD, "sync", "reports", "non_english_deleted.csv")

FR = re.compile(r"\b(le|la|les|des|une|du|au|aux|vous|nous|pour|avec|dans|est|sont|été|"
                r"pièces?|secondes?|possession|disponibles?|contre|semaine|terminer|"
                r"retour|jour|jeu|votre|tous|toutes|guilde|élite|manche|"
                r"marchandises?|actif|interne|suivant|exclusif|restants?|instable|"
                r"envahisseur|requis|acheter|épisode|succès|déverrouiller|gauche|droite|"
                r"salue|rit|pleure|prie|suit|ramasse|remercie|menace|acclame|boude|creuse|"
                r"drague|fanfaronne|sifflote|titube|tremble|tombe|parle|joue|perd|chantons|"
                r"ici|non|oui|ça|cette|cet|qui|que|quoi|mais|plus|très|bien|sur|par|son|ses|"
                r"mon|ma|mes|notre|leur|elle|ils|elles|être|avoir|faire|dire|aller)\b", re.I)
DE = re.compile(r"\b(der|die|das|und|nicht|sie|ihr|ein|eine|mit|für|auf|ist|sind|von|wird|"
                r"woche|zurück|gegen|beenden|spiel|kaufen|erforderlich|folge|freischalten)\b", re.I)
ES = re.compile(r"\b(los|las|una|para|con|por|que|más|está|son|semana|volver|contra|juego|"
                r"comprar|requiere|episodio|desbloquear)\b", re.I)
EN = re.compile(r"\b(the|and|you|your|for|with|that|this|are|was|have|will|from|not|but|all|"
                r"his|her|they|there|what|when|who|how|of|to|in|on|is|it|be|as|at|by|we|"
                r"defeated|used|given|eaten|crafted|collected|visited|completed|unlocked)\b", re.I)
DIA_FR = re.compile(r"[éèêëàâçùûîïôœÉÈÊÀÂÇÙÎÔŒ]")
DIA_DE = re.compile(r"[äöüßÄÖÜ]")
DIA_ES = re.compile(r"[áíóúñ¿¡]")
# французские хвосты команд и звуков, где служебных слов нет вовсе
FR_TAIL = re.compile(r"^/(?:boire|carte|ciseaux|danseducrabe|dire|escouade|feuille|groupe|"
                     r"pierre|poucebas|poucehaut|siffler|tremblefort|chuchoter|biceps|"
                     r"héroïque|possédé)\b|^\((?:grogne|gazouillement|jacassement|gribouillis|"
                     r"aboiement|rire|soupir)\)|^\"?(?:Wouf|Miaou|MIAOU)\b", re.I)


def verdict(en_text):
    """('fr'|'de'|'es'|'', уверенность, почему)."""
    e = en_text
    n_en = len(EN.findall(e))
    scores = {"fr": len(FR.findall(e)), "de": len(DE.findall(e)), "es": len(ES.findall(e))}
    lang = max(scores, key=scores.get)
    n = scores[lang]
    if FR_TAIL.search(e):
        return "fr", "уверенно", "команда или звук французского клиента"
    if n >= 2 and n > n_en:
        return lang, "уверенно", "служебных слов языка %d против английских %d" % (n, n_en)
    dia = ("fr" if DIA_FR.search(e) else "de" if DIA_DE.search(e) else
           "es" if DIA_ES.search(e) else "")
    if dia and n_en == 0 and n >= 1:
        return dia, "уверенно", "диакритика и служебное слово, английских слов нет"
    # одной диакритики мало: «Place the Café Register decoration» — английская
    # строка. Нужно, чтобы слов со знаками было несколько.
    acc = len(re.findall(r"[A-Za-zÀ-ÿ]*[éèêëàâçùûîïôœäöüßáíóúñ][A-Za-zÀ-ÿ]*", e))
    if dia and n_en == 0 and acc >= 2:
        return dia, "под вопросом", "несколько слов с диакритикой, английских слов нет"
    if n == 1 and n_en == 0 and dia and len(e.split()) <= 6:
        return lang, "под вопросом", "служебное слово языка и диакритика"
    return "", "", ""


def scan(paths):
    out = []
    for fp in paths:
        rows = list(csv.reader(io.open(fp, encoding="utf-8")))
        for i, r in enumerate(rows[1:], start=2):
            if not r or not r[0].strip():
                continue
            lang, conf, why = verdict(r[0])
            if lang:
                out.append((os.path.basename(fp), i, lang, conf, why, r[0]))
    return out


def batch_paths(args):
    if args:
        return [a if os.path.exists(a) else os.path.join(CROWD, a) for a in args]
    return sorted(glob.glob(os.path.join(CROWD, "new", "new_*.csv")))


def write_report(found, path=None):
    path = path or REPORT
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["файл", "строка", "язык", "уверенность", "признак", "english"])
        w.writerows(found)
    return path


def cmd_check(args):
    found = scan(batch_paths(args))
    per_lang = collections.Counter(x[2] for x in found)
    per_conf = collections.Counter(x[3] for x in found)
    per_file = collections.Counter(x[0] for x in found)
    print("найдено не-английских строк: %d" % len(found))
    print("по языкам:", dict(per_lang), "| по уверенности:", dict(per_conf))
    print("\nбольше всего в файлах:")
    for fn, n in per_file.most_common(10):
        print("   %-16s %d" % (fn, n))
    print("\nпримеры «под вопросом» (их apply НЕ трогает):")
    for x in [y for y in found if y[3] == "под вопросом"][:10]:
        print("   %-14s %s" % (x[0], x[5][:74].replace("\n", " ")))
    print("\n-> %s" % os.path.relpath(write_report(found), CROWD))


def cmd_apply(args):
    # --all добавляет «под вопросом»: их стоит удалять только после того, как
    # список прочитан глазами, поэтому по умолчанию они остаются в батчах
    take_all = "--all" in args
    args = [a for a in args if a != "--all"]
    paths = batch_paths(args)
    found = scan(paths)
    write_report(found, REPORT_APPLIED)
    kill = collections.defaultdict(set)
    for fn, line, lang, conf, why, en in found:
        if conf == "уверенно" or take_all:
            kill[fn].add(en)
    total = 0
    for fp in paths:
        fn = os.path.basename(fp)
        if fn not in kill:
            continue
        rows = list(csv.reader(io.open(fp, encoding="utf-8")))
        head, body = rows[0], rows[1:]
        keep = [r for r in body if not (r and r[0] in kill[fn])]
        n = len(body) - len(keep)
        if not n:
            continue
        with io.open(fp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(head)
            w.writerows(keep)
        print("   %-16s удалено %3d, осталось %3d" % (fn, n, len(keep)))
        total += n
    print("удалено строк: %d | список удалённого: %s"
          % (total, os.path.relpath(REPORT_APPLIED, CROWD)))
    print("не забудь пересчитать доску: python stats.py --mark-done")


CMDS = {"check": cmd_check, "apply": cmd_apply}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]](sys.argv[2:])
