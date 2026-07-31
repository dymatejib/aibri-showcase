"""СЧЁТ ДИЛЕРА — леджер дебет/кредит. Закон **L-11 «счёт как часы»**.

Владелец дословно: «надо чтоб пересчитывался и всё было работало как часы, у нас
же есть счет, все манипуляции должны на него влиять».

`amount` = изменение ДОЛГА ДИЛЕРА нам. Оплата ему деньгами → «+» (дали денег,
дилер должен товаром); приход товара от него → «−» (гасит наш платёж).
Баланс дилера = Σ amount: >0 дилер должен нам (переплатили/недовоз),
<0 мы должны дилеру.

Автопроводки (бот): приход `receipt` (−Σ qty×цена накладной), платёжка `payment`
(+), наличные `cash` (+). Ручные: `adjust` (± владелец), `opening` (стартовый
баланс до бота).

ЧТО ТАКОЕ «ТО ЖЕ СОБЫТИЕ» (тонкое место, найденное на живых деньгах): гард
идемпотентности сначала был один для всех видов — `(dealer_id, kind, order_id)`
БЕЗ суммы, то есть «один заказ = максимум одна проводка каждого вида». Но у
заказа законно бывает ДВЕ платёжки (поставщику платят частями), и вторая
становилась ТИХИМ no-op: деньги уходили из банка и не ложились на счёт вовсе.
Теперь у `payment` гард включает СУММУ, а у `receipt`/`cash` остаётся событийным
(приход по заказу один, наличные по заказу одни).

ДОЛГ ТОВАРОМ (`debt_goods`): строка приёмки «не привезли» приход по позиции НЕ
проводит, но кладёт на счёт дилера ЧТО именно и СКОЛЬКО не довезли. `amount` у
такой строки ВСЕГДА 0 — это чистые метаданные: денежная сторона уже покрыта
обычным receipt<payment (меньше привезли → меньше «−» на receipt → долг дилера
растёт сам). Двойного счёта нет.
"""
import json

from ..db import conn, one, q

KIND_META = {   # kind → (иконка, подпись, «знак вида» для показа)
    "payment": ("💸", "оплата перечислением", "money"),
    "cash": ("💵", "наличные на месте", "money"),
    "receipt": ("📦", "приход · накладная", "goods"),
    "adjust": ("✎", "корректировка", "signed"),
    "opening": ("▲", "стартовый баланс", "signed"),
    "debt_goods": ("📭", "долг товаром", "goods_debt"),
}

_AMOUNT_KINDS = ("payment",)


def _ts():
    return (one("SELECT datetime('now','localtime') t") or {}).get("t")


def post(dealer_id, kind, amount, order_id=None, note="", author="бот", idempotent=True):
    """Записать операцию. `idempotent` (для автопроводок с order_id) — повтор ТОГО ЖЕ
    события НЕ задваивает строку. Ручные adjust/opening идут с order_id=None и
    ограничением не связаны — корректировок может быть много."""
    if not dealer_id or not amount:
        return False
    with conn() as c:
        if idempotent and order_id is not None:
            sql = "SELECT 1 FROM dealer_ledger WHERE dealer_id=? AND kind=? AND order_id=?"
            args = [dealer_id, kind, order_id]
            if kind in _AMOUNT_KINDS:
                sql += " AND amount=?"
                args.append(int(amount))
            if c.execute(sql, tuple(args)).fetchone():
                return False
        c.execute(
            "INSERT INTO dealer_ledger(dealer_id,ts,kind,amount,order_id,note,author) "
            "VALUES(?,?,?,?,?,?,?)",
            (dealer_id, _ts(), kind, int(amount), order_id, note, author))
    return True


def balance(dealer_id):
    """Долг дилера нам (Σ amount): >0 дилер должен, <0 мы должны."""
    return int((one("SELECT COALESCE(SUM(amount),0) s FROM dealer_ledger WHERE dealer_id=?",
                    (dealer_id,)) or {}).get("s") or 0)


def totals(dealer_id):
    """Плитки счёта: сколько мы оплатили, сколько привезли, итоговый долг."""
    paid = int((one("SELECT COALESCE(SUM(amount),0) s FROM dealer_ledger "
                    "WHERE dealer_id=? AND kind IN ('payment','cash')", (dealer_id,))
                or {}).get("s") or 0)
    recv = -int((one("SELECT COALESCE(SUM(amount),0) s FROM dealer_ledger "
                     "WHERE dealer_id=? AND kind='receipt'", (dealer_id,))
                 or {}).get("s") or 0)
    return {"paid": paid, "received": recv, "debt": balance(dealer_id)}


def _fmt_qty(n):
    n = n or 0
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def entries(dealer_id, limit=100):
    """Лента операций (свежие сверху) с готовым видом для показа. Тон строки —
    смысловой, а не «по знаку числа»: приход товара зелёный, оплата красная,
    корректировка — по знаку долга."""
    out = []
    for r in q("SELECT * FROM dealer_ledger WHERE dealer_id=? ORDER BY id DESC LIMIT ?",
               (dealer_id, limit)):
        icon, label, mode = KIND_META.get(r["kind"], ("•", r["kind"], "signed"))
        amt = r["amount"] or 0
        if r["kind"] == "debt_goods":
            pname = (one("SELECT name FROM products WHERE id=?", (r["product_id"],)) or {}) \
                .get("name") or "?"
            qty, closed = r["qty"] or 0, r["qty_closed"] or 0
            left = round(qty - closed, 4)
            lbl = f"долг товаром: {pname} × {_fmt_qty(qty)}"
            if closed:
                lbl += f" (закрыто {_fmt_qty(closed)})"
            try:
                row_price = float((json.loads(r["note"] or "{}") or {}).get("price") or 0)
            except (ValueError, TypeError):
                row_price = 0.0
            out.append({"id": r["id"], "kind": "debt_goods", "icon": icon, "label": lbl,
                        "amount": 0, "tone": "neg" if left > 0.0001 else "pos",
                        "order_id": r["order_id"], "author": r["author"] or "",
                        "product_id": r["product_id"], "qty": qty, "qty_closed": closed,
                        "qty_left": left, "open": left > 0.0001, "price": row_price,
                        "left_value": round(left * row_price, 2)})
            continue
        if mode == "goods":            # приход товара: зелёным +сумма
            disp, tone = abs(amt), "pos"
        elif mode == "money":          # оплата: красным −сумма
            disp, tone = -abs(amt), "neg"
        else:                          # корректировка/старт: по знаку долга
            disp, tone = amt, ("pos" if amt >= 0 else "neg")
        out.append({"id": r["id"], "kind": r["kind"], "icon": icon, "label": label,
                    "amount": disp, "tone": tone, "order_id": r["order_id"],
                    "note": r["note"] or "", "author": r["author"] or ""})
    return out


# ── автопроводки из событий заказа ─────────────────────────────────────────
def _dealer_of(oid):
    return (one("SELECT dealer_id FROM orders WHERE id=?", (oid,)) or {}).get("dealer_id")


def _num_of(oid):
    return (one("SELECT num FROM orders WHERE id=?", (oid,)) or {}).get("num") or f"#{oid}"


def post_receipt(oid):
    """Приход по заказу → −Σ(qty×цена) в долг дилера (товар пришёл, гасит платёж)."""
    did = _dealer_of(oid)
    if not did:
        return False
    s = (one("SELECT COALESCE(SUM(qty*price),0) s FROM stock_moves "
             "WHERE order_id=? AND kind='receipt'", (oid,)) or {}).get("s") or 0
    if not s:
        return False
    return post(did, "receipt", -int(round(s)), order_id=oid,
                note=f"приход по заказу {_num_of(oid)}")


def post_payment(oid, amount):
    """Платёжка сошлась → +amount в долг дилера (мы заплатили)."""
    did = _dealer_of(oid)
    if not (did and amount):
        return False
    return post(did, "payment", int(round(amount)), order_id=oid,
                note=f"платёжка по заказу {_num_of(oid)}")


def settled(oid):
    """Сколько по заказу УЖЕ ПРОВЕДЕНО денег — ЕДИНСТВЕННАЯ арифметика «оплачено по
    проводкам» в проекте: её читают и сверка приёмки, и гейт наличных, и оракул
    «до оплаты склад не двигается»."""
    return int((one("SELECT COALESCE(SUM(amount),0) s FROM dealer_ledger "
                    "WHERE order_id=? AND kind IN ('payment','cash')", (oid,))
                or {}).get("s") or 0)


def post_cash(oid, total):
    """Оплата наличными на месте → в долг дилера НЕДОСТАЮЩАЯ часть, а НЕ весь `total`.

    Живой класс дефекта: заказ помечен «уже оплачено перечислением», к нему
    приложена платёжка (её проводит `post_payment`) — и следом наличная проводка
    клала ВЕСЬ итог заказа поверх. Счёт дилера задваивался. Правило теперь ПО
    ДЕНЬГАМ, а не по каналу: `max(0, total − Σ уже проведённых)`. Смешанная оплата
    (часть перечислением, остаток налом) остаётся законной и складывается."""
    did = _dealer_of(oid)
    if not did:
        return 0
    left = max(0, int(round(total)) - settled(oid))
    if left:
        post(did, "cash", left, order_id=oid, note=f"наличные по заказу {_num_of(oid)}")
    return left


def adjust(dealer_id, amount, note, author="владелец"):
    """Ручная корректировка счёта. ЕДИНСТВЕННАЯ дверь для вида `adjust`: правка
    закрытого заказа (`apply_locked_fix`) заходит сюда же, а не собирает свою
    такую же проводку рядом."""
    return post(dealer_id, "adjust", amount, order_id=None, note=note, author=author,
                idempotent=False)


def opening(dealer_id, amount, note="стартовый баланс", author="владелец"):
    """Долг, который был до бота. Вид отдельный (не `adjust`), потому что в ленте
    операций это другая строка, а в арифметике «оплачено по заказу» он НЕ
    участвует вовсе: деньгами по конкретному заказу стартовый баланс не является."""
    return post(dealer_id, "opening", amount, order_id=None, note=note, author=author,
                idempotent=False)


# ── долг товаром + довоз ───────────────────────────────────────────────────
def post_debt_goods(dealer_id, product_id, qty, price, order_id=None, author="владелец"):
    """«Не привезли N штук» — метаданные долга, `amount=0` (см. докстринг модуля)."""
    if not (dealer_id and product_id and qty):
        return None
    with conn() as c:
        cur = c.execute(
            "INSERT INTO dealer_ledger(dealer_id,ts,kind,amount,order_id,product_id,"
            "qty,qty_closed,note,author) VALUES(?,?,'debt_goods',0,?,?,?,0,?,?)",
            (dealer_id, _ts(), order_id, product_id, qty,
             json.dumps({"price": price}, ensure_ascii=False), author))
        return cur.lastrowid


def open_debt_goods_for_products(dealer_id, product_ids, exclude_order_id=None):
    """Открытые долги товаром по списку товаров — источник плашки «довоз».

    **Закон L-09 «довоз не ест свой хвост».** Плашка «довезли следующей накладной»
    на ПОВТОРНОМ рендере приёмки предлагала закрыть долги, которые СОЗДАЛА ЭТА ЖЕ
    приёмка: нажатие «Довезли» задвоило бы приход (тот же товар попал бы в остатки
    второй раз). Лечится не в UI, а здесь: вызывающая приёмка передаёт
    `exclude_order_id` — свой собственный заказ. Остальные вызовы (счёт дилера, где
    закрытие законно) идут без параметра и работают как раньше."""
    if not (dealer_id and product_ids):
        return []
    ph = ",".join("?" * len(product_ids))
    sql = ("SELECT * FROM dealer_ledger WHERE dealer_id=? AND kind='debt_goods' "
           f"AND product_id IN ({ph}) AND qty > COALESCE(qty_closed,0)")
    args = [dealer_id] + list(product_ids)
    if exclude_order_id is not None:
        sql += " AND (order_id IS NULL OR order_id != ?)"
        args.append(exclude_order_id)
    return q(sql + " ORDER BY id", tuple(args))


def close_debt_goods(ledger_id, qty_deliver):
    """Довоз: копим `qty_closed`, не трогая `amount` (он всегда 0)."""
    row = one("SELECT * FROM dealer_ledger WHERE id=? AND kind='debt_goods'", (ledger_id,))
    if not row:
        return 0
    left = float(row["qty"] or 0) - float(row["qty_closed"] or 0)
    take = max(0.0, min(float(qty_deliver or 0), left))
    if take:
        with conn() as c:
            c.execute("UPDATE dealer_ledger SET qty_closed=COALESCE(qty_closed,0)+? WHERE id=?",
                      (take, ledger_id))
    return take


# ── L-11: правка ЗАКРЫТОГО заказа обязана дойти до счёта ───────────────────
def log_event(oid, text, author="бот"):
    with conn() as c:
        c.execute("INSERT INTO order_events(order_id,author,text,ts) "
                  "VALUES(?,?,?,datetime('now','localtime'))", (oid, author, text))


def apply_locked_fix(oid, old_total, new_total, what):
    """🔒-правка закрытого заказа (цена/количество/скидка) → ОДНА проводка `adjust`
    на дельту + запись «было → стало» в историю заказа.

    Знак: итог ВЫРОС → как будто пришло больше товара → «−» на счёт (мы должны
    больше); итог УПАЛ (скидка в нашу пользу) → «+» (мы должны меньше).
    Дельта 0 (владелец нажал то же значение) → НИ ОДНОЙ новой строки: счёт не
    шумит на действиях, которые ничего не изменили.

    ⚠ Асимметрия, которая ЗАКОН, а не недоделка: правка БОНУСА счёт не двигает
    вовсе — цена бонусной позиции всегда 0 и в позиции заказа, и в движении
    склада, поэтому смена её количества не меняет ни копейки долга. Это
    доказывается отдельным тестом (отсутствие проводки), а не подразумевается."""
    did = _dealer_of(oid)
    delta = int(round(new_total)) - int(round(old_total))
    if not (did and delta):
        return 0
    adjust(did, -delta,
           f"🔒-фикс: {what} · было {int(old_total)} → стало {int(new_total)}")
    log_event(oid, f"🔒 {what}: было {int(old_total)} → стало {int(new_total)}", "бот")
    return -delta
