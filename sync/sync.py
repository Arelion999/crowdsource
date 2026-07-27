#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Синхронизация новых игровых строк из сервиса переводов в наш пайплайн.

Запускай после сдачи каждого батча:
    python sync/sync.py
    python sync/sync.py --deep     # не доверять /count: всегда тянуть /list и сверять снимок
    python sync/sync.py --force     # то же, что --deep (полный проход)

Что делает:
  1. Спрашивает /count. Если число не изменилось с прошлого раза и не --deep — выходит (быстрый путь).
  2. Тянет /list ( <hash>,<english> ) и сверяет со снимком прошлого раза sync/.snapshot.json:
       - ДОБАВЛЕНО  — новые hash'и (новые строки игры);
       - ИЗМЕНЕНО   — тот же hash, но english переписан (наш перевод старого варианта осиротел);
       - УДАЛЕНО    — hash пропал (перевод осиротел).
  3. Новые english (из добавленных и из новой формы изменённых), которых ещё нет в наших данных
     (main_strings.csv / dict_*.csv / pn_*.csv / батчи; сравнение с нормализацией пробелов),
     и которые переводимы (есть буквы) — дописывает в main_strings.csv (пустой перевод) и
     нарезает в новые батчи crowdsource/new/new_NNN.csv (продолжая нумерацию), обновляет CLAIMS.md.
  4. Изменённые/удалённые НЕ трогает автоматически (риск), а пишет отчёт sync/reports/…txt
     для ручной ревизии (какие переводы устарели / осиротели).
  5. Обновляет снимок и счётчик.

Адрес сервиса НЕ в коде и НЕ печатается: берётся из GW2_SYNC_URL или sync/endpoint.txt (в .gitignore).
"""
import csv, io, os, re, sys, json, math, glob, datetime, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE   = os.path.dirname(os.path.abspath(__file__))
CROWD  = os.path.dirname(HERE)
CYRDIR = os.path.dirname(CROWD)
def _main_file():
    for n in ("main_strings.csv", "cyrillic_strings.csv"):
        p = os.path.join(CYRDIR, n)
        if os.path.exists(p):
            return p
    return os.path.join(CYRDIR, "main_strings.csv")
CYR    = _main_file()
NEWDIR = os.path.join(CROWD, "new")
CLAIMS = os.path.join(CROWD, "CLAIMS.md")
STATE  = os.path.join(HERE, ".state.json")
SNAP   = os.path.join(HERE, ".snapshot.json")
REPDIR = os.path.join(HERE, "reports")
BATCH_SIZE = 500

def base_url():
    u = os.environ.get("GW2_SYNC_URL")
    if not u:
        p = os.path.join(HERE, "endpoint.txt")
        if os.path.exists(p):
            u = open(p, encoding="utf-8").read().strip()
    if not u:
        sys.exit("Не задан адрес сервиса. Укажи GW2_SYNC_URL или sync/endpoint.txt (в .gitignore).")
    return u.rstrip("/")

def fetch(url, timeout=180):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8")

def norm(s):
    return re.sub(r"\s+", " ", s).strip()

def translatable(s):
    return bool(re.search(r"[A-Za-z]", re.sub(r"%\w+%|<[^>]+>|\[[^\]]*\]", "", s)))

def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default

def save_json(p, d):
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)

def our_english_norm():
    seen = set()
    for fp in (glob.glob(os.path.join(CYRDIR, "dict_*.csv")) + [CYR] +
               glob.glob(os.path.join(CYRDIR, "pn_*.csv")) +
               glob.glob(os.path.join(CROWD, "*", "*.csv"))):
        try:
            with open(fp, encoding="utf-8") as f:
                r = csv.reader(f); next(r, None)
                for row in r:
                    if row:
                        seen.add(norm(row[0]))
        except Exception:
            pass
    return seen

def max_new_index():
    mx = 0
    if os.path.isdir(NEWDIR):
        for name in os.listdir(NEWDIR):
            m = re.match(r"new_(\d+)\.csv$", name)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx

def append_cyrillic(new_strings):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL).writerows([[s, ""] for s in new_strings])
    with open(CYR, "rb") as f:
        raw = f.read()
    if raw and not raw.endswith(b"\n"):
        raw += b"\n"
    open(CYR, "wb").write(raw + buf.getvalue().encode("utf-8"))

def write_batches(new_strings, start_index):
    os.makedirs(NEWDIR, exist_ok=True)
    rows = []
    for i in range(math.ceil(len(new_strings) / BATCH_SIZE)):
        chunk = new_strings[i*BATCH_SIZE:(i+1)*BATCH_SIZE]
        name = f"new_{start_index+i:03d}.csv"
        b = io.StringIO()
        ww = csv.writer(b, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        ww.writerow(["english", "translate"])
        ww.writerows([[s, ""] for s in chunk])
        open(os.path.join(NEWDIR, name), "w", encoding="utf-8", newline="").write(b.getvalue())
        sample = chunk[0].replace("\n", " ").replace("\r", " ").replace("|", "/").replace("<br>", " ")[:45]
        rows.append((name, len(chunk), sum(len(x) for x in chunk), sample))
    return rows

def update_claims(claims_rows, added):
    txt = open(CLAIMS, encoding="utf-8").read()
    m = re.search(r"Всего батчей: \*\*(\d+)\*\* \| строк: \*\*([\d,]+)\*\*", txt)
    if m:
        nb = int(m.group(1)) + len(claims_rows)
        ns = int(m.group(2).replace(",", "")) + added
        txt = txt.replace(m.group(0), f"Всего батчей: **{nb}** | строк: **{ns:,}**")
    if not txt.endswith("\n"):
        txt += "\n"
    txt += "\n".join(f"| `{n}` | new | {r} | {c:,} | {s} |  |  |" for n, r, c, s in claims_rows) + "\n"
    open(CLAIMS, "w", encoding="utf-8", newline="").write(txt)

def write_report(changed, removed):
    os.makedirs(REPDIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    p = os.path.join(REPDIR, f"changes_{ts}.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"# Отчёт синка {ts}\n")
        f.write(f"Изменённых строк (english переписан): {len(changed)}\n")
        f.write(f"Удалённых строк: {len(removed)}\n\n")
        if changed:
            f.write("== ИЗМЕНЕНО (старый english -> новый; проверь/обнови перевод) ==\n")
            for old, new in changed:
                f.write(f"- WAS: {old}\n  NOW: {new}\n")
            f.write("\n")
        if removed:
            f.write("== УДАЛЕНО (перевод осиротел; можно почистить) ==\n")
            for en in removed:
                f.write(f"- {en}\n")
    return p

def main():
    deep = ("--deep" in sys.argv) or ("--force" in sys.argv)
    url = base_url()
    state = load_json(STATE, {})
    try:
        count = int(fetch(url + "/count", timeout=30).strip())
    except Exception as e:
        sys.exit(f"Не удалось получить /count: {e}")
    if not deep and state.get("last_count") == count:
        print(f"Синхронизация: /count не изменился ({count}). "
              f"Для глубокой сверки (правки на месте) запусти с --deep.")
        return

    print(f"Сервис: {count} строк. Тяну /list...")
    raw = fetch(url + "/list")
    new_map = {}
    for row in csv.reader(io.StringIO(raw)):
        if len(row) >= 2:
            new_map[row[0]] = ",".join(row[1:])

    old_map = load_json(SNAP, {})
    added_h  = [h for h in new_map if h not in old_map]
    removed  = [old_map[h] for h in old_map if h not in new_map]
    changed  = [(old_map[h], new_map[h]) for h in new_map
                if h in old_map and old_map[h] != new_map[h]]
    if old_map:
        print(f"По снимку: добавлено {len(added_h)}, изменено {len(changed)}, удалено {len(removed)}.")
    else:
        print("Снимок отсутствовал — создаю базовый (изменения/удаления в этот раз не считаю).")

    # кандидаты на перевод: новая english-форма из добавленных и изменённых
    cand = {new_map[h] for h in added_h} | {new for _, new in changed}
    if not old_map:
        cand = set(new_map.values())  # первый снимок: сверяемся со всей таблицей
    ours = our_english_norm()
    exact = set()
    with open(CYR, encoding="utf-8") as f:
        r = csv.reader(f); next(r, None)
        for row in r:
            if row:
                exact.add(row[0])
    new = sorted({en for en in cand
                  if norm(en) not in ours and en not in exact and translatable(en)})

    if changed or removed:
        rep = write_report(changed, removed)
        print(f"Отчёт об изменённых/удалённых: {os.path.relpath(rep)}")

    if new:
        start = max_new_index() + 1
        append_cyrillic(new)
        claims_rows = write_batches(new, start)
        update_claims(claims_rows, len(new))
        print(f"Добавлено новых строк: {len(new)} (~{sum(len(x) for x in new):,} знаков)")
        print(f"Новые батчи: new_{start:03d} … new_{start+len(claims_rows)-1:03d} ({len(claims_rows)} шт.)")
    else:
        print("Новых переводимых строк для батчей нет.")

    save_json(SNAP, new_map)
    save_json(STATE, {"last_count": count})
    if new or changed or removed:
        print("Готово. Не забудь закоммитить crowdsource/ и запустить release.py.")

if __name__ == "__main__":
    main()
