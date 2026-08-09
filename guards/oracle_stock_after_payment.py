"""ОРАКУЛ «ДО ОПЛАТЫ СКЛАД НЕ ДВИГАЕТСЯ».

Проверяет не строчку кода, а ПОВЕДЕНИЕ на временной базе: заводит заказ, кормит
его деньгами по частям и смотрит, в какой момент появляются движения склада.

Что доказывается:
  O-1  неоплаченный приход НЕ пишет ни одного движения и честно называет вердикт;
  O-2  частичная оплата — всё ещё не двигает: «часть денег» не равно «часть товара»;
  O-3  оплата сошлась → приход проведён, остаток товара вырос ровно на количество;
  O-4  ПЕРЕПЛАТА останавливает так же, как недоплата (симметрия обязательна:
       пока переплата только «называлась вслух», лишние деньги тихо оседали в
       долге дилера и всплывали при ревизии месяцами позже);
  O-5  повторное проведение того же заказа не задваивает склад;
  O-6  осознанный тап владельца («провести всё равно») проводит приход, но уходит
       в историю заказа ОТ ЕГО ИМЕНИ — решение видно, а не растворяется;
  O-7  «не привезли» кладёт долг товаром и НЕ двигает склад: денежная сторона
       покрыта тем, что приход меньше оплаты, второй проводки не заводится.

Мутационный контроль (O-M): если снять гейт, оракул обязан покраснеть. Проверяем
это честно — вызовом с `force=True`, который и есть «поведение без гейта».
"""
import os
import tempfile

from ._base import Guard


def main():
    os.environ["AIBRI_DATA_DIR"] = tempfile.mkdtemp(prefix="aibri_oracle_stock_")
    from aibri import db
    db.reset_db()
    from aibri.money import ledger, receiving

    g = Guard("oracle_stock_after_payment")
    with db.conn() as c:
        c.execute("INSERT INTO dealers(id,name) VALUES(1,'ООО «NDT DISTRIBUTION»')")
        c.execute("INSERT INTO products(id,name,purchase_price,stock) "
                  "VALUES(10,'Santal BezGaz 0.50L',2500,0)")
        c.execute("INSERT INTO products(id,name,purchase_price,stock) "
                  "VALUES(11,'Choco Latto',7000,0)")
        c.execute("INSERT INTO orders(id,num,dealer_id,status,total) "
                  "VALUES(100,'#100',1,'в работе',300000)")
        c.execute("INSERT INTO orders(id,num,dealer_id,status,total) "
                  "VALUES(101,'#101',1,'в работе',420000)")
    lines = [{"product_id": 10, "qty": 120, "price": 2500}]      # 300 000

    # O-1 денег нет вовсе
    res = receiving.receive_lines(100, lines)
    g.ok("O-1 неоплаченный приход не двигает склад", res["moved"] == 0)
    g.ok("O-1 вердикт назван деньгами, а не галочкой", res["verdict"] == "payment_debt",
         str(res))
    g.ok("O-1 не хватает ровно суммы заказа", res["gap"] == 300000, str(res))
    g.ok("O-1 остаток товара нетронут", receiving.stock_of(10) == 0)

    # O-2 оплачено частично
    ledger.post_payment(100, 100000)
    res = receiving.receive_lines(100, lines)
    g.ok("O-2 частичная оплата всё ещё не двигает склад", res["moved"] == 0)
    g.ok("O-2 не хватает остатка, а не всей суммы", res["gap"] == 200000, str(res))

    # O-3 деньги сошлись
    ledger.post_payment(100, 200000)
    res = receiving.receive_lines(100, lines)
    g.ok("O-3 оплата сошлась → приход проведён", res["moved"] == 1, str(res))
    g.ok("O-3 остаток вырос ровно на количество накладной", receiving.stock_of(10) == 120)
    g.ok("O-3 приход лёг на счёт дилера минусом",
         ledger.balance(1) == 300000 - 300000, str(ledger.totals(1)))

    # O-5 повтор
    res = receiving.receive_lines(100, lines)
    g.ok("O-5 повторное проведение не задваивает склад",
         res["moved"] == 0 and receiving.stock_of(10) == 120, str(res))
    # остатка мало: он мог бы сойтись и при двух движениях, гасящих друг друга —
    # смотрим на сами движения
    g.ok("O-5 движение склада по заказу осталось ровно одно",
         len(receiving.moves_of(100)) == 1, str(receiving.moves_of(100)))

    # O-4 переплата
    ledger.post_payment(101, 500000)
    lines2 = [{"product_id": 11, "qty": 60, "price": 7000}]      # 420 000
    res = receiving.receive_lines(101, lines2)
    g.ok("O-4 переплата останавливает так же, как недоплата",
         res["moved"] == 0 and res["verdict"] == "payment_over", str(res))
    g.ok("O-4 названа величина переплаты", res["gap"] == 80000, str(res))
    g.ok("O-4 остаток товара нетронут", receiving.stock_of(11) == 0)

    # O-6 осознанный тап владельца
    res = receiving.receive_lines(101, lines2, force=True)
    g.ok("O-6 владелец может провести осознанно", res["moved"] == 1, str(res))
    g.ok("O-6 остаток вырос", receiving.stock_of(11) == 60)
    ev = db.q("SELECT * FROM order_events WHERE order_id=101")
    g.ok("O-6 решение владельца попало в историю заказа от его имени",
         any(r["author"] == "владелец" and "владельцем" in (r["text"] or "") for r in ev),
         str(ev))

    # O-7 «не привезли»
    stock_before = receiving.stock_of(10)
    lid = ledger.post_debt_goods(1, 10, qty=12, price=2500, order_id=100)
    g.ok("O-7 «не привезли» пишет долг товаром", bool(lid))
    g.ok("O-7 и НЕ двигает склад", receiving.stock_of(10) == stock_before)
    row = db.one("SELECT amount FROM dealer_ledger WHERE id=?", (lid,))
    g.ok("O-7 долг товаром — метаданные, сумма проводки 0", row["amount"] == 0, str(row))

    # O-M мутационный контроль: поведение БЕЗ гейта
    with db.conn() as c:
        c.execute("INSERT INTO orders(id,num,dealer_id,status,total) "
                  "VALUES(102,'#102',1,'в работе',300000)")
    res = receiving.receive_lines(102, lines, force=True)
    g.ok("O-M без гейта склад двигается при нулевой оплате — гейт не тавтология",
         res["moved"] == 1 and res["verdict"] == "payment_debt", str(res))
    return g.report()


if __name__ == "__main__":
    main().exit()
