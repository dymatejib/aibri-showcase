"""ПРИЁМКА: гейт «склад двигается только после денег».

Один из самых дорогих уроков проекта. Раньше «оплачено» решала ГАЛОЧКА в
интерфейсе, а не деньги: заказ с уже проведённой платёжкой при выключенном
тумблере не закрывался, и бухгалтеру уходило поручение оплатить уже оплаченное.
Обратная ошибка того же корня — приход проводился на склад по одному тапу, пока
денежная сторона ещё не сошлась, и остатки «росли» раньше, чем магазин
расплатился.

Правило: факт оплаты берётся ИЗ ПРОВОДОК (`ledger.settled`), а не из флага, и
пока он не сошёлся с суммой прихода — движения склада НЕ ПИШУТСЯ. Тумблер
остаётся ручной галочкой владельца, но судья — деньги.

Симметрия обязательна: НЕДОПЛАТА и ПЕРЕПЛАТА одинаково останавливают заказ.
Пока переплата только «называлась вслух» и ехала дальше, лишние деньги тихо
оседали в долге дилера и всплывали при ревизии месяцами позже.

Строка «не привезли» на предэкране приёмки при этом закон НЕ нарушает: она не
двигает склад вовсе — она пишет долг товаром (`ledger.post_debt_goods`,
`amount=0`), а деньги остаются на общей арифметике receipt<payment.
"""
from ..db import conn, one, q
from . import ledger, tolerance


def payment_verdict(oid, total):
    """('ok' | 'payment_debt' | 'payment_over', сколько не хватает/сколько лишку).

    Считает по ПРОВОДКАМ, а не по галочке. Допуск НЕ ЖИВЁТ ЗДЕСЬ: он берётся из
    `tolerance.pay_eps()` — единственного дома всех порогов сравнения денег. Своей
    цифры допуска приёмка не заводит, иначе их станет две."""
    eps = tolerance.pay_eps()
    paid = ledger.settled(oid)
    delta = paid - int(round(total))
    if delta < -eps:
        return "payment_debt", -delta
    if delta > eps:
        return "payment_over", delta
    return "ok", 0


def receive_lines(oid, lines, force=False):
    """Провести приход. Возвращает `{"moved": N, "verdict": ..., "gap": ...}`.

    `moved=0` при неоплаченном заказе — и это не ошибка вызова, а закон:
    вызывающий экран показывает владельцу вердикт и сумму, а не молча пишет склад.
    `force=True` — осознанный тап владельца («провести всё равно»), он уходит в
    историю заказа ОТ ЕГО ИМЕНИ, а не растворяется."""
    total = sum(int(x.get("qty") or 0) * float(x.get("price") or 0) for x in lines)
    verdict, gap = payment_verdict(oid, total)
    if verdict != "ok" and not force:
        return {"moved": 0, "verdict": verdict, "gap": gap, "total": int(total)}
    if has_receipt(oid):                    # идемпотентность: приход по заказу ОДИН
        return {"moved": 0, "verdict": "already", "gap": 0, "total": int(total)}
    moved = 0
    with conn() as c:
        for x in lines:
            qty = float(x.get("qty") or 0)
            if qty <= 0:
                continue
            c.execute(
                "INSERT INTO stock_moves(order_id,product_id,kind,qty,price,note,ts) "
                "VALUES(?,?,'receipt',?,?,?,datetime('now','localtime'))",
                (oid, x["product_id"], qty, float(x.get("price") or 0),
                 "провёл владелец вручную" if force else ""))
            c.execute("UPDATE products SET stock=COALESCE(stock,0)+? WHERE id=?",
                      (qty, x["product_id"]))
            moved += 1
    if force and verdict != "ok":
        ledger.log_event(oid, f"приход проведён владельцем при вердикте «{verdict}»",
                         "владелец")
    ledger.post_receipt(oid)
    return {"moved": moved, "verdict": verdict, "gap": gap, "total": int(total)}


def has_receipt(oid):
    return bool(one("SELECT 1 FROM stock_moves WHERE order_id=? AND kind='receipt' LIMIT 1",
                    (oid,)))


def stock_of(pid):
    return float((one("SELECT stock FROM products WHERE id=?", (pid,)) or {}).get("stock") or 0)


def moves_of(oid):
    return q("SELECT * FROM stock_moves WHERE order_id=? ORDER BY id", (oid,))
