"""СЧЁТ ДИЛЕРА «КАК ЧАСЫ» (L-11).

Владелец дословно: «надо чтоб пересчитывался и всё было работало как часы, у нас
же есть счет, все манипуляции должны на него влиять».

Проверяется не «функция вызвана», а СЧЁТ: после каждой манипуляции баланс равен
тому, что человек посчитал бы на бумаге.
"""
import pytest

from aibri.money import ledger


@pytest.fixture()
def db(fresh_db):
    with fresh_db.conn() as c:
        c.execute("INSERT INTO dealers(id,name) VALUES(1,'ООО «NDT DISTRIBUTION»')")
        c.execute("INSERT INTO products(id,name,purchase_price,stock) "
                  "VALUES(10,'Santal BezGaz 0.50L',2500,0)")
        c.execute("INSERT INTO orders(id,num,dealer_id,status,total) "
                  "VALUES(100,'#100',1,'закрыт',300000)")
        c.execute("INSERT INTO stock_moves(order_id,product_id,kind,qty,price) "
                  "VALUES(100,10,'receipt',120,2500)")
    return fresh_db


def test_приход_гасит_платёж(db):
    ledger.post_payment(100, 300000)
    assert ledger.balance(1) == 300000        # заплатили — дилер должен товаром
    ledger.post_receipt(100)
    assert ledger.balance(1) == 0             # привезли — счёт закрыт
    assert ledger.totals(1) == {"paid": 300000, "received": 300000, "debt": 0}


def test_вторая_платёжка_по_заказу_ложится_второй_строкой(db):
    """Поставщику платят частями — это норма. Пока гард идемпотентности не знал про
    СУММУ, вторая платёжка была тихим no-op: деньги ушли из банка и не легли на
    счёт вовсе."""
    assert ledger.post_payment(100, 100000) is True
    assert ledger.post_payment(100, 200000) is True
    assert ledger.settled(100) == 300000
    assert len(db.q("SELECT 1 FROM dealer_ledger WHERE kind='payment'")) == 2


def test_точный_повтор_платёжки_не_задваивает(db):
    ledger.post_payment(100, 100000)
    assert ledger.post_payment(100, 100000) is False
    assert ledger.settled(100) == 100000


def test_наличные_кладут_только_недостающее(db):
    """Раньше наличная проводка клала ВЕСЬ итог заказа поверх уже проведённой
    платёжки, и счёт дилера задваивался. Правило теперь по деньгам, а не по каналу."""
    ledger.post_payment(100, 200000)
    left = ledger.post_cash(100, 300000)
    assert left == 100000
    assert ledger.settled(100) == 300000


def test_всё_покрыто_наличной_проводки_нет(db):
    ledger.post_payment(100, 300000)
    assert ledger.post_cash(100, 300000) == 0
    assert len(db.q("SELECT 1 FROM dealer_ledger WHERE kind='cash'")) == 0


def test_стартовый_баланс_не_считается_оплатой_заказа(db):
    """Долг «до бота» на общем счёте виден, но деньгами ПО ЗАКАЗУ не является:
    иначе гейт «склад двигается только после денег» открылся бы от старого долга,
    и приход прошёл бы без единой копейки за него."""
    ledger.opening(1, 500000, "долг до бота")
    assert ledger.balance(1) == 500000
    assert ledger.settled(100) == 0                    # к заказу отношения не имеет
    assert ledger.totals(1)["paid"] == 0               # и оплатой не считается
    ledger.post_payment(100, 300000)
    assert ledger.settled(100) == 300000 and ledger.balance(1) == 800000


# ── правка закрытого заказа доходит до счёта ───────────────────────────────
def test_скидка_на_закрытом_заказе_меняет_счёт(db):
    ledger.post_payment(100, 300000)
    ledger.post_receipt(100)
    assert ledger.balance(1) == 0
    delta = ledger.apply_locked_fix(100, 300000, 270000, "скидка 10 %")
    assert delta == 30000                     # итог упал → мы должны меньше
    assert ledger.balance(1) == 30000


def test_повтор_той_же_правки_не_шумит_на_счёте(db):
    ledger.apply_locked_fix(100, 300000, 270000, "скидка 10 %")
    before = len(db.q("SELECT 1 FROM dealer_ledger"))
    assert ledger.apply_locked_fix(100, 270000, 270000, "скидка 10 %") == 0
    assert len(db.q("SELECT 1 FROM dealer_ledger")) == before


def test_правка_пишет_было_стало_в_историю(db):
    ledger.apply_locked_fix(100, 300000, 312000, "цена строки")
    ev = db.q("SELECT * FROM order_events WHERE order_id=100")
    assert ev and "было 300000 → стало 312000" in ev[0]["text"]
    note = db.one("SELECT note FROM dealer_ledger WHERE kind='adjust'")["note"]
    assert "было 300000 → стало 312000" in note


def test_рост_итога_и_падение_дают_противоположные_знаки(db):
    assert ledger.apply_locked_fix(100, 300000, 312000, "цена") == -12000
    assert ledger.apply_locked_fix(100, 312000, 300000, "откат") == 12000
    assert ledger.balance(1) == 0             # обратимость: нетто ноль


def test_бонус_счёт_не_двигает(db):
    """Асимметрия, которая ЗАКОН, а не пропущенный случай: цена бонусной позиции
    всегда ноль, поэтому смена её количества не меняет ни копейки долга.
    Доказывается ОТСУТСТВИЕМ проводки, а не подразумевается."""
    ledger.apply_locked_fix(100, 300000, 300000, "бонус 6 → 12 шт")
    assert db.q("SELECT 1 FROM dealer_ledger WHERE kind='adjust'") == []


# ── долг товаром и довоз ───────────────────────────────────────────────────
def test_долг_товаром_не_деньги(db):
    lid = ledger.post_debt_goods(1, 10, qty=12, price=2500, order_id=100)
    assert db.one("SELECT amount FROM dealer_ledger WHERE id=?", (lid,))["amount"] == 0
    assert ledger.balance(1) == 0
    e = [x for x in ledger.entries(1) if x["kind"] == "debt_goods"][0]
    assert e["qty_left"] == 12 and e["left_value"] == 30000 and e["open"] is True


def test_довоз_не_ест_свой_хвост(db):
    """Плашка «довезли следующей накладной» предлагала закрыть долг, который
    СОЗДАЛА ЭТА ЖЕ приёмка: нажатие задвоило бы приход. Лечится не в интерфейсе, а
    параметром, который приёмка передаёт про свой собственный заказ."""
    ledger.post_debt_goods(1, 10, qty=12, price=2500, order_id=100)
    assert ledger.open_debt_goods_for_products(1, [10], exclude_order_id=100) == []
    assert len(ledger.open_debt_goods_for_products(1, [10], exclude_order_id=999)) == 1
    assert len(ledger.open_debt_goods_for_products(1, [10])) == 1   # в счёте виден


def test_частичный_довоз_копится(db):
    lid = ledger.post_debt_goods(1, 10, qty=12, price=2500, order_id=100)
    assert ledger.close_debt_goods(lid, 5) == 5
    assert ledger.close_debt_goods(lid, 100) == 7      # больше долга не закроет
    assert ledger.open_debt_goods_for_products(1, [10]) == []


def test_лента_операций_красит_по_смыслу_а_не_по_знаку(db):
    ledger.post_payment(100, 300000)
    ledger.post_receipt(100)
    rows = {e["kind"]: e for e in ledger.entries(1)}
    assert rows["payment"]["amount"] == -300000 and rows["payment"]["tone"] == "neg"
    assert rows["receipt"]["amount"] == 300000 and rows["receipt"]["tone"] == "pos"
