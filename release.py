#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка релиза: вливает переводы из всех готовых батчей в файлы папки glyphCore/
(main_strings.csv + dict_*.csv), которые читает игра, и версионирует результат
локальным git-коммитом в папке glyphCore/.

Запуск (без аргументов — пути определяются автоматически):
    python release.py
    python release.py --no-commit    # только собрать, без git-коммита

Пути:
    - папка glyphCore = родительская папка этого репозитория (на уровень выше);
    - готовые батчи   = сам этот репозиторий (crowdsource/), рекурсивно все *.csv.

Обёртка над merge_back.py: заполняет ТОЛЬКО пустые строки (никогда не перезаписывает
уже переведённое), проверяет совпадение плейсхолдеров/тегов, хранит концы строк и UTF-8.
Идемпотентно: повторный запуск без новых переводов заполнит 0 строк.

Версионирование: glyphCore/ — отдельный ЛОКАЛЬНЫЙ git-репозиторий (crowdsource/ и bak/
исключены). После сборки делается `git add -A` + commit со сводкой. Так каждый релиз
(и добавленные синком строки) попадает в историю и откатывается при необходимости.
"""
import os, sys, subprocess, datetime

HERE = os.path.dirname(os.path.abspath(__file__))   # .../crowdsource
CYR  = os.path.dirname(HERE)                          # .../glyphCore

sys.path.insert(0, HERE)
import merge_back  # noqa: E402
import validate    # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def gate():
    """Линт батчей перед сборкой. Возвращает True, если можно продолжать."""
    checked, errors, warnings = validate.validate_paths([HERE])
    print(f"Валидация: проверено {checked:,} строк, ошибок {len(errors)}, предупреждений {len(warnings)}.")
    if errors:
        for f, ln, en, m in errors[:30]:
            print(f"  ОШИБКА {f}:{ln}  «{en}» — {m}")
        if len(errors) > 30:
            print(f"  … и ещё {len(errors)-30}")
    return not errors


def git(*args):
    return subprocess.run(["git", "-C", CYR, *args],
                          capture_output=True, text=True)


def commit_release():
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("git: glyphCore/ не под версионированием — коммит пропущен "
              "(инициализируй репозиторий, если нужно).")
        return
    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("git: изменений для коммита нет.")
        return
    # краткая статистика по застейдженным файлам
    stat = git("diff", "--cached", "--stat").stdout.strip().splitlines()
    tail = stat[-1] if stat else ""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = (f"release {ts}: сборка переводов в glyphCore\n\n{tail}\n\n"
           "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")
    r = git("commit", "-m", msg)
    if r.returncode == 0:
        print(f"git: закоммичено ({ts}). {git('rev-parse','--short','HEAD').stdout.strip()}")
    else:
        print("git: коммит не удался:\n" + (r.stderr or r.stdout))


if __name__ == "__main__":
    do_commit = "--no-commit" not in sys.argv
    force = "--force" in sys.argv
    if not gate() and not force:
        print("Релиз остановлен: сначала исправь ошибки выше (или запусти с --force).")
        sys.exit(1)
    sys.argv = ["merge_back.py", CYR, HERE]
    merge_back.main()
    if do_commit:
        commit_release()
