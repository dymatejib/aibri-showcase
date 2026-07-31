"""СВЕРКА КАССОВОЙ СМЕНЫ. Закон **L-21 «три расхождения никогда не складываются»**.

У магазина ТРИ РАЗНЫХ расхождения. У каждого свой смысл, свой знак и свой адресат,
и ни одна витрина не имеет права сложить их — ни в число, ни в один цвет.

① **ДЕНЬГИ СМЕНЫ** — «общая сумма за смену − аппарат №1 − аппарат №2 − наличка =
   остаток». Это про кассира и эквайринг. Светофор по зонам владельца:
   0–50 тыс зелёная · 50–100 тыс жёлтая · 100 тыс+ красная.
② **МИМО ККМ** — «карта аппаратов ↔ карта Z-отчёта». Это про ФИСКАЛИЗАЦИЮ и
   налоговую, к кассиру отношения НЕ имеет → НЕЙТРАЛЬНЫЙ синий, БЕЗ зоны и БЕЗ
   слова «ошибка». Наличка ККМ — ПАМЯТКА: ни зоны, ни вердикта.
③ **ФИЗКАССА** — «наличка на начало → наличка на конец» (пересчёт ящика). Излишки
   и недостачи показываются ПОРОЗНЬ: на рабочем месяце излишки и недостачи были
   ОДНОГО ПОРЯДКА, а нетто выходило на порядок меньше каждого из них — почерк
   РАЗМЕНА, а не воровства, и увидеть его можно ТОЛЬКО не складывая.

Почему это закон, а не вкусовщина: пока три числа стояли в одном ряду, экран врал
в ОБЕ стороны. Старая таблица показывала расхождение карты как ⚠-ошибку у
подавляющего большинства смен — хотя это нейтральное «мимо ККМ». А месячная колонка
кассовой программы несёт В ОДНОЙ ЯЧЕЙКЕ и физкассу (сотни тысяч), и миллионное
«мимо ККМ»: сложи их — и месяц показывает многомиллионную недостачу там, где её нет.

СПУТНИК ЗАКОНА — «НЕ ПРОВЕРЕНО (НЕТ БУМАГ)» ≠ «РАСХОЖДЕНИЕ». Смена без слипов
терминалов не получает ни остатка, ни зоны, ни красного: вычитать нечего, и это
вопрос сбора фотографий, а не кассира.

⚠ Отдельный класс — ПУСТАЯ СМЕНА: открыта и закрыта за минуты, выручки нет вовсе.
Её расхождение НЕЛЬЗЯ мешать с настоящими: у таких строк в поле «остаток в кассе»
регулярно стоит опечатка пересчёта (условно 300 вместо 3 000 000), и одна такая
строка дала бы месяцу фальшивую многомиллионную недостачу при нулевой выручке и
смене длиной в полминуты.
"""
from ..recognition import corpus

ZONE_GREEN_MAX = 50_000        # зоны владельца дословно: «0-50 зелёная»
ZONE_AMBER_MAX = 100_000       # «50-100 жёлтая» · «100к+ красная»
EMPTY_SHIFT_MAX_SEC = 15 * 60


def zone(value):
    """Зона расхождения ПО ДЕНЬГАМ СМЕНЫ / ФИЗКАССЕ.

    ГРАНИЦЫ: нижняя ВКЛЮЧАЕТСЯ, верхняя нет — [0, 50к) зелёная, [50к, 100к) жёлтая,
    [100к, ∞) красная. Ровно так звучит владелец: «100к+ красная», и ровно 100 000
    обязано быть красным. Раньше верхняя граница была строгой в одну сторону и
    нестрогой в другую (`> 100к` красная при `>= 50к` жёлтой), и сотня тысяч ровно
    попадала в жёлтую — то есть в единственной точке, которую владелец назвал вслух,
    светофор отвечал не то. Границы закрыты параметризованным тестом.

    ⚠ К «мимо ККМ» НЕ применяется — то расхождение нейтральное. Светофор в проекте
    ОДИН, и это он: второй завести нельзя, страж проверяет."""
    a = abs(int(value or 0))
    if a >= ZONE_AMBER_MAX:
        return "r"
    if a >= ZONE_GREEN_MAX:
        return "a"
    return "g"


def terminals():
    """АППАРАТЫ МАГАЗИНА — ДАННЫМИ (`doc_patterns`, pattern_kind='terminal').
    Новый аппарат = СТРОКА В ТАБЛИЦУ, без правки этого файла; «№N» — порядок строк,
    а не хардкод номера в коде витрины."""
    out = []
    for i, r in enumerate(corpus.patterns("zreport", "terminal")):
        no = str(r["key"] or "").split(":", 1)[-1].strip()
        label = (r["value"] or "").strip()
        chans = []
        if "·" in label:
            chans = [c.strip().lower() for c in label.split("·", 1)[1].split("/") if c.strip()]
        out.append({"no": no, "name": f"Аппарат №{i + 1}", "label": label, "channels": chans})
    return out


def _channel_terminal(channel, terms):
    """Канал слипа → номер аппарата ПО СЛОВАРЮ ДАННЫХ. Канал, для которого аппарат
    не назван, честно НЕ приписывается никому — он уйдёт в отдельную строку «прочие
    каналы», а не молча в аппарат №1 «чтобы сошлось»."""
    ch = (channel or "").strip().lower()
    if not ch:
        return ""
    for t in terms:
        for c in t["channels"]:
            if ch == c or ch.startswith(c) or c.startswith(ch):
                return t["no"]
    return ""


def split_slips(slips, terms=None):
    """Слипы смены, разложенные по аппаратам. `slips` — [(канал, сумма)]."""
    terms = terms if terms is not None else terminals()
    by_no = {t["no"]: 0 for t in terms}
    chans = {t["no"]: [] for t in terms}
    other, other_chans = 0, []
    for ch, amt in (slips or []):
        no = _channel_terminal(ch, terms)
        if no in by_no:
            by_no[no] += int(amt or 0)
            chans[no].append((ch, int(amt or 0)))
        else:
            other += int(amt or 0)
            other_chans.append((ch, int(amt or 0)))
    rows = [{"no": t["no"], "name": t["name"], "amount": by_no[t["no"]],
             "channels": sorted(chans[t["no"]], key=lambda kv: -kv[1])} for t in terms]
    total = sum(r["amount"] for r in rows) + other
    return {"rows": rows, "other": other, "other_channels": other_chans,
            "sum": total, "has": bool(slips and total)}


def is_empty_shift(blk):
    """Смена без выручки, открытая и закрытая за минуты — отдельный класс (см.
    докстринг модуля). Из списка НЕ прячется, но её ± уходит в отдельную видимую
    строку «± пустых смен — проверить пересчёт», а не в общий счёт."""
    if int(blk.get("revenue") or 0):
        return False
    o, c = blk.get("opened_ts"), blk.get("closed_ts")
    if o is None or c is None:
        return False
    return (c - o) <= EMPTY_SHIFT_MAX_SEC


# ── ① ДЕНЬГИ СМЕНЫ ─────────────────────────────────────────────────────────
def shift_money(blk, slips=None, terms=None):
    """СТОЛБИК СВЕРКИ в порядке владельца дословно:
        ОБЩАЯ СУММА ЗА СМЕНУ − аппарат №1 − аппарат №2 [− прочие] − наличка
        = ОСТАТОК (светофор по зонам).

    ⚠ НАЛИЧКА здесь — «что кассир принял наличными» по чекам. Пересчёт ЯЩИКА —
    ТРЕТИЙ, отдельный счёт, в этот столбик он не входит.
    ⚠ БУМАГ НЕТ → ОСТАТКА НЕТ: без слипов вычитать нечего, состояние «не проверено»,
    и в сумму расхождений смена не попадает."""
    terms = terms if terms is not None else terminals()
    total = int(blk.get("revenue") or 0)
    cash = int(blk.get("cash") or 0)
    sl = split_slips(slips, terms)
    rest = (total - sl["sum"] - cash) if sl["has"] else None
    return {"total": total, "cash": cash, "card": total - cash,
            "app": sl["rows"], "other": sl["other"], "slips_sum": sl["sum"],
            "verified": sl["has"], "rest": rest,
            "state": zone(rest) if sl["has"] else "mut",
            "unverified": None if sl["has"] else total - cash}


# ── ② МИМО ККМ ─────────────────────────────────────────────────────────────
def shift_kkm(blk, z_row=None, slips=None, terms=None):
    """Карта Z-отчёта ↔ карта аппаратов (с фото). Разница = НЕФИСКАЛИЗИРОВАННАЯ
    КАРТА: нейтральный синий, без зоны и без слова «ошибка».

    ⚠ Здесь СОЗНАТЕЛЬНО не вызывается `zone()` — страж класса это проверяет
    греп-ассертом по телу функции. Стоит один раз покрасить это расхождение в
    красный, и владелец начнёт искать виноватого кассира там, где вопрос к
    фискализации."""
    terms = terms if terms is not None else terminals()
    if not z_row:
        return {"has_z": False, "gap": None, "state": "mut",
                "label": "НЕ ПРОВЕРЕНО · Z-отчёт за смену ещё не разобран", "chips": []}
    sl = split_slips(slips, terms)
    kkm_card = int(z_row.get("card") or 0)
    kkm_cash = int(z_row.get("cash") or 0)
    gap = (sl["sum"] - kkm_card) if sl["has"] else None
    if gap is None:
        label, state = "НЕ ПРОВЕРЕНО · нет бумаг аппаратов", "mut"
    elif gap == 0:
        label, state = "СХОДИТСЯ", "g"
    elif gap > 0:
        label, state = "МИМО ККМ", "b"
    else:
        label, state = "НЕ СХОДИТСЯ · слипы не полные", "b"
    chips = [{"text": f"Z-отчёт №{z_row.get('z_no')}",
              "kind": "g" if z_row.get("confident") else "a"}]
    for row in sl["rows"]:
        if not row["channels"]:
            chips.append({"text": f"{row['name']} — бумаги нет", "kind": "mut"})
    return {"has_z": True, "z_no": z_row.get("z_no"), "kkm_card": kkm_card,
            "kkm_cash": kkm_cash, "term_sum": sl["sum"], "verified": sl["has"],
            "gap": gap, "label": label, "state": state, "chips": chips}


# ── ③ ФИЗКАССА ─────────────────────────────────────────────────────────────
def shift_cashbox(blk):
    """«Наличка на начало → наличка на конец» (пересчёт ящика). Ни слипов, ни ККМ
    здесь нет по построению.

    Есть ФАКТ пересчёта — считаем расхождение и красим по зонам. Факта нет
    (программа его перестала писать) — показываем РАСЧЁТ и честно говорим, что это
    расчёт. `has_fact=False` — это НЕ расхождение."""
    if blk.get("cash_close") is None:
        return {"has_fact": False, "diff": None, "state": "mut",
                "note": "пересчёта наличных за смену нет — это РАСЧЁТ, а не факт"}
    expected = (int(blk.get("cash_open") or 0) + int(blk.get("cash") or 0)
                - int(blk.get("given") or 0) + int(blk.get("received") or 0))
    diff = int(blk.get("cash_close") or 0) - expected
    label = ("РАСХОЖДЕНИЕ · ИЗЛИШЕК" if diff > 0 else
             ("РАСХОЖДЕНИЕ · НЕДОСТАЧА" if diff < 0 else "СОШЛОСЬ"))
    return {"has_fact": True, "open": int(blk.get("cash_open") or 0),
            "close": int(blk.get("cash_close") or 0), "expected": expected,
            "diff": diff, "state": zone(diff), "label": label,
            "empty_shift": is_empty_shift(blk),
            "note": "факт — пересчёт кассира на закрытии смены"}


def shift_verdict(money, kkm, box):
    """ОДНОСТРОЧНЫЙ ВЕРДИКТ + чипы. Три счёта в чипах ПОРОЗНЬ — они не
    складываются. В теле этой функции нет и не может быть арифметики вида
    `rest + gap` или `rest + diff`: страж класса проверяет это грепом."""
    chips = []
    if money["verified"]:
        rest = int(money["rest"] or 0)
        kind, state, amount = ("ok" if rest == 0 else "bad"), money["state"], rest
        chips.append({"label": "остаток", "kind": state, "amount": rest})
    else:
        kind, state, amount = "unchecked", "mut", None
        chips.append({"label": "нет слипов аппаратов", "kind": "mut", "amount": None})
    if kkm.get("has_z") and kkm.get("gap"):
        # Подпись берётся У САМОГО СЧЁТА, а не пишется здесь второй раз. Пока чип
        # знал только слово «мимо ККМ», ОТРИЦАТЕЛЬНЫЙ разрыв (слипов меньше, чем
        # карты в Z-отчёте) он тоже называл «мимо ККМ» — а это противоположный
        # случай, и `shift_kkm` его уже назвал правильно: «слипы не полные».
        chips.append({"label": kkm["label"], "kind": "b", "amount": kkm["gap"]})
    elif not kkm.get("has_z"):
        chips.append({"label": "нет Z-отчёта", "kind": "mut", "amount": None})
    if box.get("has_fact"):
        chips.append({"label": "физкасса", "kind": box["state"],
                      "amount": int(box.get("diff") or 0)})
    else:
        chips.append({"label": "пересчёта наличных нет", "kind": "mut", "amount": None})
    return {"state": state, "kind": kind, "amount": amount, "chips": chips}


def shift_recon(blk, z_row=None, slips=None):
    """Полная сверка ОДНОЙ смены: три счёта порознь + вердикт."""
    terms = terminals()
    money = shift_money(blk, slips, terms)
    kkm = shift_kkm(blk, z_row, slips, terms)
    box = shift_cashbox(blk)
    return {"money": money, "kkm": kkm, "box": box,
            "verdict": shift_verdict(money, kkm, box)}


def month_totals(shifts):
    """Месяц: те же три счёта СУММАМИ, и снова порознь. Излишки и недостачи
    физкассы копятся РАЗДЕЛЬНО — иначе размен выглядит как воровство. Смены без
    бумаг считаются отдельной строкой «не проверено», а не нулём."""
    out = {"rest": 0, "kkm_gap": 0, "box_plus": 0, "box_minus": 0,
           "unverified": 0, "empty_plus": 0, "empty_minus": 0, "n": 0}
    for rec in shifts:
        out["n"] += 1
        money, kkm, box = rec["money"], rec["kkm"], rec["box"]
        if money["verified"]:
            out["rest"] += int(money["rest"] or 0)
        else:
            out["unverified"] += int(money.get("unverified") or 0)
        if kkm.get("gap"):
            out["kkm_gap"] += int(kkm["gap"])
        if box.get("has_fact"):
            d = int(box.get("diff") or 0)
            key = ("empty_plus" if d > 0 else "empty_minus") if box.get("empty_shift") \
                else ("box_plus" if d > 0 else "box_minus")
            if d:
                out[key] += d
    return out
