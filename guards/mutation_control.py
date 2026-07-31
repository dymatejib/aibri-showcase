"""МУТАЦИОННЫЙ КОНТРОЛЬ ЭТАЛОНОВ — страж, который проверяет самих стражей.

Оракул, который не падает на подложенной ошибке, — не оракул. Его зелёный цвет
не значит ничего, а стоит он дороже отсутствия: он создаёт уверенность.

Поэтому механизм эталонов обязан ПАДАТЬ на двух диверсиях:
  · «все цены и суммы ÷ 10» — потеря разряда в деньгах;
  · «qty + 1 на каждой строке», цена пересчитана из суммы так, что
    тавтологическая самопроверка «кол-во × цена = сумма» остаётся зелёной.

Вторая диверсия — главная. Именно так выглядит порча, от которой склад тихо растёт,
и именно её НЕ ВИДИТ агрегатная метрика `ok_lines`: она считает строки, где
арифметика сходится, а сходится она по построению.

Здесь же печатается разрыв между слабой метрикой (`ok_lines`) и честной
(`ok_read` — количество и сумма напечатаны на одной физической строке документа).
Разрыв — не косметика: пока качество мерили слабой метрикой, ПОРЧА ПАРСЕРА ЕЁ
УЛУЧШАЛА.
"""
import os
import tempfile

from ._base import Guard


def main():
    # Каталог создаём, ТОЛЬКО если его не задали снаружи: `setdefault` вычисляет
    # аргумент всегда и оставлял пустой временный каталог на каждом прогоне даже
    # там, где переменная уже стояла.
    if not os.environ.get("AIBRI_DATA_DIR"):
        os.environ["AIBRI_DATA_DIR"] = tempfile.mkdtemp(prefix="aibri_mutation_")
    from aibri import db
    db.init_db()
    from aibri.recognition import golden as G

    g = Guard("mutation_control")
    texts = G.load_corpus()
    parsed = G.parse_all(texts)
    doc = G.load_golden()
    base_metrics = G.measure(parsed)
    base_ok = G.golden_score(parsed, doc["docs"])
    want = doc["baseline"]

    print(f"  корпус: документов {base_metrics['docs'] + base_metrics['none']} · "
          f"разобрано {base_metrics['docs']} · строк {base_metrics['lines']} · "
          f"ok_lines {base_metrics['ok_lines']} (слабая) · "
          f"ok_read {base_metrics['ok_read']} (честная) · "
          f"Σ строк = печатный итог {base_metrics['sum_eq_total']} из "
          f"{base_metrics['with_total']}")

    # ВТОРОЙ монотонный гейт — на сам корпус эталонов. Без него первый гейт можно
    # «выполнить», убрав из golden неудобную строку: число совпадений не упадёт,
    # потому что упадёт знаменатель. Строк эталонов имеет право становиться только
    # больше.
    paper_lines = sum(len(d["lines"]) for d in doc["docs"])
    g.ok(f"эталонов строк не меньше базлайна ({want['lines']})",
         paper_lines >= want["lines"], f"сейчас {paper_lines}")
    g.ok(f"эталоны: «= бумага» не ниже базлайна ({want['ok']})", base_ok >= want["ok"],
         f"сейчас {base_ok}")
    if base_ok > want["ok"] or paper_lines > want["lines"]:
        print(f"  🟢 УЛУЧШЕНИЕ: «= бумага» {base_ok} при базлайне {want['ok']}, строк "
              f"эталонов {paper_lines} при {want['lines']} — обнови baseline в "
              f"golden_lines.json")

    # честный отчёт о долге: строки, которые бот сегодня не читает
    for d in doc["docs"]:
        r = parsed.get(d["key"], (None, None))[1]
        gr = G.grade(d, (r or {}).get("lines") or [])
        for item in gr["debt"]:
            p = item["paper"]
            if item["state"] == "lost":
                print(f"  🔴 ДОЛГ {d['key']} №{p['no']} «{p['name']}» — СТРОКИ НЕТ В "
                      f"РАЗБОРЕ; бумага: {p['qty']} × {p['price']} = {p['sum']}")
            else:
                print(f"  🔴 ДОЛГ {d['key']} №{p['no']} «{p['name']}» — бумага "
                      f"{p['qty']}×{p['price']}={p['sum']}, бот {item['got']}")

    for kind, human in (("div10", "все цены и суммы ÷ 10"),
                        ("qty1", "qty +1 на каждой строке (цена пересчитана, ok сохранён)")):
        mut = G.mutate(parsed, kind)
        mm = G.measure(mut)
        mok = G.golden_score(mut, doc["docs"])
        print(f"  диверсия «{human}»: ok_lines {mm['ok_lines']} (было "
              f"{base_metrics['ok_lines']}) · ok_read {mm['ok_read']} (было "
              f"{base_metrics['ok_read']}) · эталоны «= бумага» {mok} (было {base_ok})")
        g.ok(f"диверсия «{kind}» ловится построчными эталонами ({mok} < {base_ok})",
             mok < base_ok)
        g.ok(f"диверсия «{kind}» ловится честной метрикой ok_read "
             f"({mm['ok_read']} < {base_metrics['ok_read']})",
             mm["ok_read"] < base_metrics["ok_read"])

    # почему слабой метрики недостаточно — доказываем, а не утверждаем
    for kind in ("div10", "qty1"):
        mm = G.measure(G.mutate(parsed, kind))
        g.ok(f"диверсия «{kind}» НЕ ловится слабой метрикой ok_lines "
             f"({mm['ok_lines']} = {base_metrics['ok_lines']}) — поэтому «кол-во × "
             f"цена = сумма» не может быть судьёй качества",
             mm["ok_lines"] >= base_metrics["ok_lines"])
    return g.report()


if __name__ == "__main__":
    main().exit()
