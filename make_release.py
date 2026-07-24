#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пакует локализацию из cyrillic/ в архив для GitHub-релиза (русификатор).

    python make_release.py                 # версия = дата (v2026.07.24)
    python make_release.py v2026.07.24     # своя версия/тег

Кладёт в архив только файлы, нужные игроку (не bak/, не crowdsource/, не логи):
    main_strings.csv, dict_*.csv, pn_*.csv, config.ini + INSTALL.txt
Результат: <родитель cyrillic>/gw2-ru-<версия>.zip — его прикрепляют ассетом к релизу.
Печатает готовые команды для публикации.
"""
import os, sys, glob, zipfile, datetime, hashlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE   = os.path.dirname(os.path.abspath(__file__))     # crowdsource
CYRDIR = os.path.dirname(HERE)                            # cyrillic
OUTDIR = os.path.dirname(CYRDIR)                          # родитель (…/Guild Wars 2)

INSTALL = """Русификатор Guild Wars 2 (GlyphCore)

Установка:
1. Установи прокси-локализатор GlyphCore: https://github.com/nourlie/GlyphCore
2. Скопируй файлы из этого архива в папку cyrillic/ рядом с GlyphCore
   (заменив существующие).
3. Запусти игру через GlyphCore.

Содержимое: main_strings.csv, dict_*.csv, pn_*.csv, config.ini.
"""

def files_to_pack():
    fs = [os.path.join(CYRDIR, "main_strings.csv"),
          os.path.join(CYRDIR, "cyrillic_strings.csv"),
          os.path.join(CYRDIR, "config.ini")]
    fs += sorted(glob.glob(os.path.join(CYRDIR, "dict_*.csv")))
    fs += sorted(glob.glob(os.path.join(CYRDIR, "pn_*.csv")))
    return [f for f in fs if os.path.exists(f)]

def main():
    ver = None
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            ver = a
    if not ver:
        ver = "v" + datetime.date.today().strftime("%Y.%m.%d")
    fs = files_to_pack()
    zip_path = os.path.join(OUTDIR, f"gw2-ru-{ver}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in fs:
            z.write(f, os.path.basename(f))
        z.writestr("INSTALL.txt", INSTALL)
    size = os.path.getsize(zip_path)
    sha = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()
    print(f"Собран архив: {zip_path}")
    print(f"  файлов: {len(fs)+1} | размер: {size/1_048_576:.1f} МБ")
    print(f"  sha256: {sha}")
    print("\nОпубликовать релиз (crowdsource — public repo):")
    print("  1) git push origin main")
    print(f"  2a) через gh CLI:")
    print(f"      gh release create {ver} \"{zip_path}\" -t \"Русификатор GW2 {ver}\" -n \"Локализация на {ver}.\"")
    print(f"  2b) без gh — через сайт:")
    print(f"      открой https://github.com/K13or/crowdsource/releases/new")
    print(f"      тег {ver}, перетащи в ассеты: {zip_path}")

if __name__ == "__main__":
    main()
