"""СТРАЖ СЦЕНАРИЯ: код не может быть новее описания поведения.

Правило владельца: каждая глубинная правка поведения ДОЛЖНА быть записана в
SCENARIO.md. Само по себе это благое пожелание — поэтому оно исполняемое.

Проверяет:
  A. SCENARIO.md существует, содержит обязательные разделы и якоря-инварианты
     (формулировки, которые нельзя потерять при переписывании).
  B. СВЕЖЕСТЬ: файлы поведения не новее SCENARIO.md больше, чем на грейс-период.
     Новее — КРАСНЫЙ и поимённый список того, что изменилось и не задокументировано.
  C. Журнал решений владельца непуст и держит формат.

Цикл работы: страж красный → дописываем сценарий → снова зелёный. Грейс-период
(по умолчанию час) существует, чтобы страж не падал внутри одной сессии работы,
когда файл поведения уже изменён, а абзац сценария пишется следом.

ОТКУДА БЕРЁТСЯ «КОГДА ФАЙЛ ИЗМЕНИЛСЯ». Не из `mtime` — по крайней мере не всегда:
`git clone` выставляет ВСЕМ файлам время выкачки, и проверка B у постороннего
человека была бы зелёной всегда, чем бы ни отличались файлы. Поэтому в чистом
рабочем дереве берётся время последнего КОММИТА, тронувшего файл, а `mtime`
остаётся для файлов, изменённых прямо сейчас, и для случая «git недоступен или это
не репозиторий». Источник времени страж печатает — проверка, про которую не
известно, что она проверяла, не проверка.
"""
import os
import subprocess
import time

from ._base import ROOT, Guard, norm, read

SCENARIO = "SCENARIO.md"
GRACE_SEC = int(os.getenv("SCENARIO_GRACE_SEC", str(60 * 60)))

REQUIRED_SECTIONS = [
    "Границы безопасности",
    "Распознавание накладной",
    "Приёмка и деньги",
    "Кассовые смены",
    "Журнал решений владельца",
]
REQUIRED_ANCHORS = [
    "склад двигается только после денег",   # гейт приёмки
    "три расхождения",                      # закон сверки смен
    "правку владельца стереть нельзя",      # закон корпуса
    "честно прочитанное важнее",            # принцип парсера
]
BEHAVIOUR_DIRS = ("aibri",)


def _git(*args):
    """stdout git-а в дереве витрины; None — git недоступен, это не репозиторий
    или команда не отработала. Страж не имеет права падать из-за отсутствия git."""
    try:
        r = subprocess.run(("git", "-C", ROOT) + args, capture_output=True,
                           text=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def dirty_paths():
    """Пути, изменённые в рабочем дереве (у них время коммита ничего не значит).
    None — git-а нет, работаем целиком по файловым временам."""
    out = _git("status", "--porcelain")
    if out is None:
        return None
    paths = set()
    for ln in out.splitlines():
        if len(ln) > 3:
            paths.add(ln[3:].split(" -> ")[-1].strip().strip('"'))
    return paths


def changed_at(rel, dirty):
    """Когда файл изменился НА САМОМ ДЕЛЕ: коммит для чистого файла, `mtime` для
    изменённого в дереве и для всего сразу, если git недоступен."""
    if dirty is not None and rel not in dirty:
        out = _git("log", "-1", "--format=%ct", "--", rel)
        if out and out.strip().isdigit():
            return int(out.strip())
    return os.path.getmtime(os.path.join(ROOT, *rel.split("/")))


def main():
    g = Guard("scenario_guard")
    path = os.path.join(ROOT, SCENARIO)
    if not g.ok("SCENARIO.md на месте", os.path.exists(path)):
        return g.report()
    text = read(SCENARIO)
    flat = norm(text)                    # перенос строки не должен ломать поиск
    for s in REQUIRED_SECTIONS:
        g.ok(f"раздел «{s}» описан", norm(s) in flat)
    for a in REQUIRED_ANCHORS:
        g.ok(f"якорь-инвариант «{a}» не потерян", norm(a) in flat)

    dirty = dirty_paths()
    scen_ts = changed_at(SCENARIO, dirty)
    stale = []
    for d in BEHAVIOUR_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, d)):
            if "__pycache__" in dirpath:
                continue
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")
                if changed_at(rel, dirty) > scen_ts + GRACE_SEC:
                    stale.append(rel)
    g.ok("сценарий не отстаёт от кода поведения", not stale,
         "не задокументировано: " + ", ".join(sorted(stale)[:10]))

    journal = text.split("Журнал решений владельца", 1)[-1]
    entries = [ln for ln in journal.splitlines() if ln.strip().startswith("- **")]
    g.ok("журнал решений владельца непуст", bool(entries))
    g.ok("журнал держит формат «- **дата · тема:**»",
         all("·" in e for e in entries))
    src = ("git: коммиты (чистые файлы) + правки рабочего дерева" if dirty is not None
           else "файловые времена: git недоступен или это не репозиторий")
    print(f"  (грейс-период {GRACE_SEC} с; источник времени — {src}; сценарий обновлён "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(scen_ts))})")
    return g.report()


if __name__ == "__main__":
    main().exit()
