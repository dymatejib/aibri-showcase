"""Общая обвязка стражей: печать проверок и код выхода. Одна на все стражи —
второго формата отчёта в проекте нет."""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Отчёт стража печатается галочками «✅/❌». На Windows поток вывода, ПЕРЕНАПРАВЛЕННЫЙ
# в файл или в пайп, берёт кодировку локали (cp1251/cp1252), и печать значка роняет
# страж UnicodeEncodeError-ом — то есть страж падает не по делу, а по кодировке.
# Просим utf-8 с заменой; там, где поток подменён (pytest) или переоткрыть нельзя,
# оставляем как есть.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass


class Guard:
    def __init__(self, title):
        self.title = title
        self.checks = []
        self.fails = []

    def ok(self, name, cond, detail=""):
        cond = bool(cond)
        self.checks.append(cond)
        why = f" — {detail}" if detail and not cond else ""   # подробность только у провала
        print(("  ✅ " if cond else "  ❌ ") + name + why)
        if not cond:
            self.fails.append(name)
        return cond

    def report(self):
        print(f"ИТОГ {self.title}: {sum(self.checks)}/{len(self.checks)}")
        if self.fails:
            print("ПРОВАЛЫ:", "; ".join(self.fails))
        return self

    def exit(self):
        """Код выхода по итогу. Отчёт уже напечатан `report()` — второй раз не печатаем."""
        sys.exit(1 if self.fails else 0)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


_TRIPLE = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')


def _docstring_spans(src):
    """Строки, занятые ТОЛЬКО докстрингами (модуль · класс · функция), по дереву
    разбора: (первая, последняя) включительно."""
    tree = ast.parse(src)
    holders = [tree] + [n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef))]
    spans = []
    for node in holders:
        body = getattr(node, "body", None) or []
        if not body:
            continue
        head = body[0]
        if isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant) \
                and isinstance(head.value.value, str):
            spans.append((head.lineno, head.end_lineno))
    return spans


def code_only(src):
    """Исходник без документации и комментариев.

    Грепстражу нельзя смотреть на прозу: в этом проекте докстринги НАЗЫВАЮТ
    запрещённые конструкции по имени («здесь стоял DELETE FROM doc_corpus», «zone()
    сюда не применяется»), и наивный греп краснел бы именно на объяснении того,
    почему так делать нельзя.

    Докстринги вырезаются ПО ДЕРЕВУ РАЗБОРА (`ast`), а не регуляркой по тройным
    кавычкам. Класс дыры, ради которого это переписано: регулярка резала ЛЮБОЙ
    тройной литерал, поэтому запрет «DELETE FROM doc_corpus» обходился одной
    строчкой — `SQL = \"\"\"DELETE FROM doc_corpus …\"\"\"` уезжал из поля зрения
    стража, и тот оставался зелёным. Обычные строковые литералы — в том числе
    многострочный SQL — ОСТАЮТСЯ: по ним стражи как раз и ищут.

    Ограничение честно: решётка внутри строки обрежет хвост строки. Для наших
    сигнатур это безопасно, для новых — проверять."""
    src = src or ""
    try:
        spans = _docstring_spans(src)
    except (SyntaxError, ValueError):     # не разобралось — грубый запасной путь
        return "\n".join(re.sub(r"#.*$", "", ln)
                         for ln in _TRIPLE.sub(" ", src).splitlines())
    lines = src.splitlines()
    for first, last in spans:
        for i in range(first - 1, min(last, len(lines))):
            lines[i] = ""
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in lines)


def norm(text):
    """Текст в одну строку с одиночными пробелами и в нижнем регистре — чтобы
    поиск формулировки не зависел от того, где редактор перенёс строку."""
    return re.sub(r"\s+", " ", (text or "")).lower()


def walk_py(*rels):
    """Все .py указанных подкаталогов — (относительный путь, текст).

    Путь отдаётся ЧЕРЕЗ «/» на любой ОС: списки разрешённых мест у грепстража
    записаны в этом виде, а `os.path.relpath` на Windows вернул бы
    `aibri\\money\\money.py` — allowlist перестал бы совпадать, и страж покраснел бы
    на ровном месте, на всём проекте сразу."""
    out = []
    for rel in rels:
        base = os.path.join(ROOT, rel)
        for dirpath, _dirs, files in os.walk(base):
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    out.append((os.path.relpath(p, ROOT).replace(os.sep, "/"), f.read()))
    return out
