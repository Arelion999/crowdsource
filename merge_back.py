#!/usr/bin/env python3
"""
Вливает готовые батчи перевода обратно в игровые файлы GlyphCore.

Использование:
    python merge_back.py <путь_к_папке_glyphCore> <папка_или_файл_с_готовыми_батчами>
    python merge_back.py <...> --upgrade   # ещё и вытеснить сломанные переводы батчевыми
    python merge_back.py <...> --report    # выписать расхождения в CSV на просмотр

Пример:
    python merge_back.py "C:/Games/Guild Wars 2/glyphCore" ./completed

Что делает:
  - собирает все переводы (english -> translate) из готовых батч-CSV;
  - в каждом игровом файле (dict_*.csv, main_strings.csv) заполняет ПУСТЫЕ строки,
    чей english совпадает (заполняет все дубли сразу);
  - с --upgrade перезаписывает и НЕпустые, но только когда батчевый перевод объективно
    лучше: у нынешнего есть ошибки линтера, а у батчевого их меньше, либо в игре лежит
    копия английского, а в батче настоящий русский текст. Остальные расхождения
    (просто другой текст) не трогает — это вкусовщина, её решает человек;
  - проверяет, что набор плейсхолдеров/тегов в переводе совпадает с оригиналом
    (иначе строку пропускает и пишет в отчёт);
  - сохраняет исходные концы строк (CRLF/LF) и кодировку UTF-8;
  - никогда не перезаписывает уже переведённые строки.
"""
import csv, io, re, glob, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                                            # noqa: E402

CYR  = re.compile(r'[а-яёА-ЯЁ]')
LATW = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
PN_WORDS = set()          # слова из pn_*.csv: их прокси подставит сама, латиница там норма


def load_pn(cyr_dir):
    for f in glob.glob(os.path.join(cyr_dir, 'pn_*.csv')):
        with open(f, encoding='utf-8-sig', newline='') as fh:
            for r in csv.reader(fh):
                if r:
                    PN_WORDS.update(w.lower() for w in LATW.findall(r[0]))
    print(f"Слой имён: {len(PN_WORDS):,} слов (латиница в них — не ошибка)")

# Обязательны к сохранению: %подстановки%, <теги>, литеральные скобки [lbracket]/[rbracket]/[null].
# А вот [s] / [pl:"..."] (мн. число) ПРАВИЛЬНО превращаются в русское [форма1|форма2|форма3],
# поэтому в проверку токенов их НЕ включаем.
TOK = re.compile(r'%\w+%|<[^>]+>|\[lbracket\]|\[rbracket\]|\[null\]')

def tokens(s):
    return sorted(TOK.findall(s))

def load_completed(path):
    """path — файл .csv или папка с .csv (рекурсивно). Возвращает {english: translate}."""
    files = []
    if os.path.isdir(path):
        files = glob.glob(os.path.join(path, '**', '*.csv'), recursive=True)
    else:
        files = [path]
    m = {}
    dup_conflict = 0
    skipped = 0
    for p in files:
        with open(p, encoding='utf-8-sig', newline='') as f:
            r = csv.reader(f)
            header = next(r, None)
            # батч — это CSV с заголовком «english,translate». Отчёты и прочие
            # служебные таблицы (напр. sync/reports/batch_conflicts.csv) пропускаем.
            if not header or header[0].strip().lower() != 'english':
                skipped += 1
                continue
            for row in r:
                if len(row) < 2:
                    continue
                en, ru = row[0], row[1]
                if not ru.strip():
                    continue
                if en in m and m[en] != ru:
                    dup_conflict += 1
                    continue
                m.setdefault(en, ru)
    print(f"Загружено переводов: {len(m):,} из {len(files) - skipped} файлов "
          f"(конфликтов дублей проигнорировано: {dup_conflict}"
          + (f", пропущено не-батчей: {skipped}" if skipped else "") + ")")
    return m

def better(en, cur, new):
    """Стоит ли заменить нынешний перевод cur батчевым new? Только объективные случаи."""
    if cur.strip() == en.strip() and CYR.search(new):
        return 'в игре была копия английского'
    ec = len(validate.check_row(en, cur)[0])
    if ec and len(validate.check_row(en, new)[0]) < ec:
        return 'нынешний перевод сломан, батчевый чище'
    # Латинское слово, которого нет в слое имён, прокси не подставит — игрок увидит
    # «гонке на roller beetle». Если батч его перевёл — берём батч.
    orphan = {w.lower() for w in LATW.findall(cur)} - {w.lower() for w in LATW.findall(new)}
    if CYR.search(new) and orphan - PN_WORDS:
        return 'в игре осталась латиница вне слоя имён: ' + ', '.join(sorted(orphan - PN_WORDS)[:3])
    # Потерянное число из оригинала («Complete 15 laps» -> «Завершите круги»).
    nums = set(re.findall(r'\d+', en))
    if nums - set(re.findall(r'\d+', cur)) and nums & set(re.findall(r'\d+', new)):
        return 'в игре потеряно число из оригинала'
    return None


def merge_file(fn, tmap, report, upgrade=False, conflicts=None):
    raw = open(fn, 'rb').read()
    lt = '\r\n' if b'\r\n' in raw else '\n'
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8'))))
    # защита: пере-сериализация без изменений должна дать байт-в-байт оригинал
    buf = io.StringIO(); csv.writer(buf, lineterminator=lt).writerows(rows)
    if buf.getvalue().encode('utf-8') != raw:
        print(f"  ПРОПУСК {os.path.basename(fn)}: не проходит round-trip (не трогаю)")
        return 0
    changed = upgraded = 0
    for r in rows:
        if len(r) < 2:
            continue
        ru = tmap.get(r[0])
        if not ru:
            continue
        if r[1].strip():                       # ячейка занята
            if not upgrade or ru == r[1]:
                continue
            why = better(r[0], r[1], ru)
            if not why:
                if conflicts is not None:
                    conflicts.append((os.path.basename(fn), r[0], r[1], ru))
                continue
            if tokens(r[0]) != tokens(ru):
                continue
            r[1] = ru
            upgraded += 1
            continue
        if tokens(r[0]) != tokens(ru):
            report.append((os.path.basename(fn), r[0][:60]))
            continue
        r[1] = ru
        changed += 1
    if changed or upgraded:
        buf = io.StringIO(); csv.writer(buf, lineterminator=lt).writerows(rows)
        open(fn, 'wb').write(buf.getvalue().encode('utf-8'))
    return changed, upgraded

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    upgrade = '--upgrade' in sys.argv
    do_report = '--report' in sys.argv
    cyr_dir, completed = args[0], args[1]
    if upgrade:
        load_pn(cyr_dir)
    tmap = load_completed(completed)
    if not tmap:
        print("Нет переводов для вливания."); return
    targets = sorted(glob.glob(os.path.join(cyr_dir, 'dict_*.csv')))
    for main in ('main_strings.csv', 'cyrillic_strings.csv'):
        p = os.path.join(cyr_dir, main)
        if os.path.exists(p):
            targets.append(p)
    total = up_total = 0
    report, conflicts = [], ([] if (do_report or upgrade) else None)
    for fn in targets:
        if not os.path.exists(fn):
            continue
        n, u = merge_file(fn, tmap, report, upgrade, conflicts)
        if n or u:
            print(f"  {os.path.basename(fn)}: +{n}" + (f", заменено {u}" if u else ""))
        total += n; up_total += u
    print(f"\nИТОГО заполнено строк: {total:,}")
    if upgrade:
        print(f"Заменено сломанных переводов батчевыми: {up_total:,}")
    if conflicts:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'sync', 'reports', 'batch_conflicts.csv')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['file', 'english', 'в игре', 'в батче'])
            w.writerows(conflicts)
        print(f"Расхождения «просто другой текст» (решает человек): {len(conflicts):,}"
              f"\n  -> {out}")
    if report:
        print(f"Пропущено из-за несовпадения плейсхолдеров: {len(report)} "
              f"(проверьте %str1%/теги в этих переводах):")
        for f, s in report[:30]:
            print(f"  [{f}] {s}")

if __name__ == '__main__':
    main()
