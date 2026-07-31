"""ГРЕПСТРАЖ «ОДИН СМЫСЛ = ОДНА РЕАЛИЗАЦИЯ».

Правило дубля из кодекса проекта: найден второй экземпляр механизма — это блокер.
Сначала слить, потом грепстраж, чтобы третий не завёлся.

Почему грепом, а не «код-ревью заметит». Формат денег в проекте разъезжался ДЕВЯТЬ
раз, и заметили только тогда, когда один из вариантов начал разделять тысячи
неразрывным пробелом — глазами, на экране. Двухтаповая защита от случайного нажатия
жила в двенадцати реализациях с четырьмя разными таймаутами. Пересчёт «пачка →
штуки» — в трёх. Ни один человек этого не удержит; регулярное выражение удержит.

Грепстраж обязан быть УЗКИМ. Наивный шаблон даёт ложные срабатывания на похожих
именах и приучает людей его игнорировать — а игнорируемый страж хуже отсутствующего.
Поэтому каждая проверка ниже ищет конкретную сигнатуру и ведёт явный список
разрешённых мест (allowlist), а не «слово где-то в файле».
"""
import re

from ._base import Guard, code_only, walk_py

# (человеческое имя, регулярка, файлы, где реализация РАЗРЕШЕНА)
RULES = [
    ("форматирование денег (разделитель тысяч)",
     re.compile(r"""replace\(\s*["'],["']\s*,\s*["'] ["']\s*\)"""),
     {"aibri/money/money.py"}),
    ("светофор зон расхождения",
     re.compile(r"^def zone\(", re.M),
     {"aibri/shifts/recon.py"}),
    ("склейка разорванных по ячейкам денег",
     re.compile(r"_MONEY_JOIN_RE\s*=", re.M),
     {"aibri/recognition/paper_invoice.py"}),
    ("арифметика «оплачено по проводкам»",
     re.compile(r"kind IN \('payment','cash'\)"),
     {"aibri/money/ledger.py"}),
    ("правило допуска расхождения",
     re.compile(r"^def covered\(", re.M),
     {"aibri/money/tolerance.py"}),
    ("свёртка ошибок OCR",
     re.compile(r"^def ocr_fold\(", re.M),
     {"aibri/recognition/corpus.py"}),
    # Обе строки ниже добавлены ПОСЛЕ того, как дубль нашёлся: разбор Z-отчёта
    # держал свою копию «числа за лейблом» (регулярки уже разъехались), а
    # мутационный контроль — свою копию формулы `ok`. Правило дубля требует не
    # только слить, но и закрыть дорогу третьему экземпляру.
    ("чтение значения поля (число сразу за лейблом)",
     re.compile(r"^def num_after\(", re.M),
     {"aibri/recognition/corpus.py"}),
    ("самопроверка строки «кол-во × цена = сумма»",
     re.compile(r"^def line_ok\(", re.M),
     {"aibri/recognition/paper_invoice.py"}),
]

# Прямое обращение к базе мимо единственного слоя доступа.
FORBIDDEN = [
    ("sqlite3.connect мимо слоя db.py", re.compile(r"sqlite3\.connect"), {"aibri/db.py"}),
    ("DELETE FROM doc_corpus (закон «правку владельца стереть нельзя»)",
     re.compile(r"DELETE\s+FROM\s+doc_corpus", re.I), set()),
]


def main():
    g = Guard("grep_guard_single_impl")
    # смотрим на КОД, а не на прозу: докстринги этого проекта называют запрещённые
    # конструкции по имени, и наивный греп краснел бы на объяснении запрета
    files = [(p, code_only(t)) for p, t in walk_py("aibri") if "__pycache__" not in p]
    g.ok("исходники найдены", bool(files))
    for name, rx, allow in RULES:
        hits = {p for p, t in files if rx.search(t)}
        extra = hits - allow
        g.ok(f"«{name}» — ровно один дом: {', '.join(sorted(allow))}",
             hits and not extra, f"лишние места: {', '.join(sorted(extra))}")
    for name, rx, allow in FORBIDDEN:
        hits = {p for p, t in files if rx.search(t)} - allow
        g.ok(f"запрещено: {name}", not hits, f"найдено в: {', '.join(sorted(hits))}")

    # Нейтральность «мимо ККМ» — греп ВНУТРИ функции, а не по файлу: светофор к
    # этому расхождению применять нельзя, иначе владелец пойдёт искать виноватого
    # кассира там, где вопрос к фискализации.
    recon = dict(files)["aibri/shifts/recon.py"]
    body = recon.split("def shift_kkm(", 1)[1].split("\ndef ", 1)[0]
    g.ok("в теле shift_kkm нет вызова zone( — «мимо ККМ» нейтрально",
         "zone(" not in body)
    body_v = recon.split("def shift_verdict(", 1)[1].split("\ndef ", 1)[0]
    g.ok("в теле shift_verdict нет сложения трёх счетов",
         not re.search(r"rest\s*\+\s*(gap|diff)|gap\s*\+\s*diff", body_v))
    body_m = recon.split("def shift_money(", 1)[1].split("\ndef ", 1)[0]
    g.ok("в теле shift_money нет ни ККМ, ни физкассы",
         not re.search(r"kkm_card|cash_close|cash_diff", body_m))
    return g.report()


if __name__ == "__main__":
    main().exit()
