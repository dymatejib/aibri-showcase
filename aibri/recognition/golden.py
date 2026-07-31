"""ЭТАЛОНЫ И МУТАЦИОННЫЙ КОНТРОЛЬ — механизм, который проверяет сам себя.

Проблема, ради которой это написано. У рабочих накладных не было НИ ОДНОГО
построчного эталона: качество мерили агрегатными порогами вроде «доля
строк, где кол-во × цена = сумма». Такой порог доказанно ПРОПУСКАЕТ 10-кратную
ошибку в деньгах на КАЖДОЙ строке — потому что цена вычисляется как сумма/кол-во,
и равенство выполняется тождественно. Хуже: когда парсер портился и начинал
вычислять больше чисел, метрика РОСЛА.

Отсюда три слоя:

  §A ДИФФ-ГЕЙТ КОРПУСА — монотонные метрики по всему набору документов
     (разобрано / строк / честно прочитано / Σ строк = печатный итог). Правка
     парсера обязана не уронить ни одну.
  §B ПОСТРОЧНЫЕ ЭТАЛОНЫ — qty/цена/сумма, выписанные ГЛАЗАМИ С БУМАГИ. Виды,
     которые сегодня читаются неверно, лежат ЧЕСТНО-КРАСНЫМИ: каждая такая строка
     печатается с ожидаемым значением бумаги и с тем, что бот выдаёт сейчас.
     Гейты монотонные — число совпавших строк НЕ ИМЕЕТ ПРАВА уменьшиться.
  §C **МУТАЦИОННЫЙ КОНТРОЛЬ** — оракул ОБЯЗАН ПАДАТЬ на диверсиях:
       · «все цены и суммы ÷ 10»  — деньги в десять раз меньше;
       · «qty + 1 на каждой строке» (цена пересчитана, тавтология сохранена).
     Не падает — оракул слепой, и все его зелёные ничего не значат.

Диверсия применяется к РЕЗУЛЬТАТУ разбора, парсер при этом не патчится: так
проверяется именно чувствительность метрики, а не поведение кода.

Почему сопоставление бумаги с разбором ГЛОБАЛЬНО ЖАДНОЕ: построчная жадность на
документе с почти одинаковыми именами вызывает каскад (две строки отличаются одним
словом, распознавание вдобавок путает букву — и верный разбор всех строк
показывается как «три строки врут», хотя ни одно число не разошлось). Совпадение
ВСЕХ ТРЁХ чисел даёт решающий бонус: имя из OCR может отличаться от бумаги, числа
— нет. Значения при этом НЕ подгоняются: бонус даётся только за ТОЧНОЕ равенство,
поэтому диверсии его теряют и по-прежнему ловятся.
"""
import json
import os
import re

from . import paper_invoice as pi

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "fixtures", "invoices")


# ── корпус фикстур ─────────────────────────────────────────────────────────
def load_corpus(folder=FIXTURES):
    """{имя файла без расширения: дословный текст}."""
    out = {}
    for fn in sorted(os.listdir(folder)):
        if fn.endswith(".txt"):
            with open(os.path.join(folder, fn), encoding="utf-8") as f:
                out[fn[:-4]] = f.read()
    return out


def load_golden(folder=FIXTURES):
    with open(os.path.join(folder, "golden_lines.json"), encoding="utf-8") as f:
        return json.load(f)


def parse_all(texts):
    return {k: (t, pi.parse_invoice(t)) for k, t in sorted(texts.items())}


# ── §A метрики корпуса ─────────────────────────────────────────────────────
def measure(parsed):
    """Метрики набора. `ok_lines` — слабая (тавтологическая), `ok_read` — честная:
    количество и сумма напечатаны на одной физической строке документа. Обе
    печатаются рядом, чтобы разрыв между ними был виден в одном экране."""
    m = {"docs": 0, "none": 0, "lines": 0, "ok_lines": 0, "ok_read": 0,
         "sum_eq_total": 0, "with_total": 0}
    for _k, (text, r) in parsed.items():
        if not r:
            m["none"] += 1
            continue
        m["docs"] += 1
        m["lines"] += len(r["lines"])
        m["ok_lines"] += sum(1 for x in r["lines"] if x.get("ok"))
        rowvals = pi.row_value_sets(text)
        m["ok_read"] += sum(1 for x in r["lines"] if pi.line_read(x, rowvals))
        cands = pi.total_candidates(text)
        if cands:
            m["with_total"] += 1
            ls = sum(x.get("sum") or 0 for x in r["lines"])
            if any(abs(ls - c) <= 2 for c in cands):
                m["sum_eq_total"] += 1
    return m


# ── §B сопоставление бумаги с разбором ─────────────────────────────────────
def _norm(s):
    return re.sub(r"[^a-zа-я0-9]", "", (s or "").lower().replace("ё", "е"))


def _name_score(paper_name, parsed_name):
    pn, xn = _norm(paper_name), _norm(parsed_name)
    if not pn or not xn:
        return 0
    if pn == xn:
        return 10000
    if pn in xn or xn in pn:
        return 5000 + min(len(pn), len(xn))
    j = 0
    while j < min(len(pn), len(xn)) and pn[j] == xn[j]:
        j += 1
    return j if j >= 6 else 0


def _nums_eq(paper, x):
    """Все три числа бумаги совпали с разбором ТОЧНО (по int, тийины отброшены)."""
    got = 0
    for fld in ("qty", "price", "sum"):
        if paper.get(fld) is None:
            continue
        v = x.get(fld)
        if v is None or int(v) != int(paper[fld]):
            return False
        got += 1
    return got >= 2


def match(paper_lines, parsed_lines):
    """Пары (индекс бумаги → индекс разбора), глобально жадно по счёту."""
    pairs = []
    for pi_idx, pl in enumerate(paper_lines):
        for xi, x in enumerate(parsed_lines):
            sc = _name_score(pl.get("name"), x.get("name"))
            if _nums_eq(pl, x):
                sc += 20000           # числа доказывают строку вернее любых букв
            if sc:
                pairs.append((sc, pi_idx, xi))
    pairs.sort(reverse=True)
    taken_p, taken_x, out = set(), set(), {}
    for _sc, p, x in pairs:
        if p in taken_p or x in taken_x:
            continue
        taken_p.add(p)
        taken_x.add(x)
        out[p] = x
    return out


def grade(doc, parsed_lines):
    """Свести бумагу и разбор: сколько строк «= бумага», сколько врёт, сколько
    вообще не появилось. Возвращает и построчную расшифровку долга — чтобы красное
    было ЧЕСТНО-красным, с ожидаемым значением рядом."""
    paper = doc["lines"]
    pairs = match(paper, parsed_lines)
    ok, debt = 0, []
    for i, pl in enumerate(paper):
        xi = pairs.get(i)
        if xi is None:
            debt.append({"no": pl.get("no"), "name": pl["name"], "state": "lost",
                         "paper": pl, "got": None})
            continue
        x = parsed_lines[xi]
        if _nums_eq(pl, x):
            ok += 1
        else:
            debt.append({"no": pl.get("no"), "name": pl["name"], "state": "wrong",
                         "paper": pl,
                         "got": {"qty": x.get("qty"), "price": x.get("price"),
                                 "sum": x.get("sum")}})
    return {"ok": ok, "total": len(paper), "debt": debt}


# ── §C диверсии ────────────────────────────────────────────────────────────
def mutate(parsed, kind):
    """Диверсия поверх РЕЗУЛЬТАТА разбора (парсер не патчится).

    `div10` — все цены и суммы в десять раз меньше: классическая «потеря разряда»,
    которую агрегатные метрики не видят вовсе.
    `qty1`  — количество +1 на каждой строке, цена пересчитана из суммы, чтобы
    тавтологическая самопроверка `ok` осталась зелёной. Именно так выглядит порча,
    от которой склад тихо растёт."""
    out = {}
    for k, (text, r) in parsed.items():
        if not r:
            out[k] = (text, r)
            continue
        lines = []
        for x in r["lines"]:
            y = dict(x)
            if kind == "div10":
                if y.get("price"):
                    y["price"] = int(y["price"] // 10)
                if y.get("sum"):
                    y["sum"] = int(y["sum"] // 10)
            elif kind == "qty1":
                y["qty"] = (y.get("qty") or 0) + 1
                if y.get("sum") and y["qty"]:
                    y["price"] = round(y["sum"] / y["qty"])
            else:
                raise ValueError("неизвестная диверсия: %s" % kind)
            # Самопроверку строки пересчитываем ТОЙ ЖЕ формулой, что и парсер —
            # буквально его функцией, а не копией: копия молча разошлась бы, и
            # доказательство «слабая метрика диверсию не видит» превратилось бы в
            # доказательство «две формулы совпали». Обе диверсии оставляют `ok`
            # ЗЕЛЁНЫМ — вот почему «кол-во × цена = сумма» не судья качества.
            y["ok"] = pi.line_ok(y)
            lines.append(y)
        out[k] = (text, dict(r, lines=lines))
    return out


def golden_score(parsed, docs):
    """Сколько строк эталонов совпало с бумагой на данном (возможно, изувеченном)
    разборе — это и есть число, которое диверсия обязана уронить."""
    total = 0
    for d in docs:
        r = parsed.get(d["key"], (None, None))[1]
        total += grade(d, (r or {}).get("lines") or [])["ok"]
    return total
