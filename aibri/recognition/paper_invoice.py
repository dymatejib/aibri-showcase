"""ПАРСЕР БУМАЖНЫХ НАКЛАДНЫХ — детерминированный разбор дословного текста фото.

Разделение слоёв, которое держит весь проект: снять с фотографии ДОСЛОВНЫЙ текст —
задача распознавания (локальный OCR, при неудаче LLM-транскрипция). ВСЕ правила
разбора — здесь, в обычном питоне, без единого похода в сеть. LLM никогда не
считает деньги и не решает, что приходовать: она только читает буквы.

Что документ приносит и почему это трудно:
  · фото снято под углом, строки таблицы склеены с соседними бумагами в кадре;
  · разряд тысяч разорван по отдельным ячейкам («| 70 | 308,00 |» = 70 308,00);
  · количество печатается «10.000000», а сумма — «1 250 000.00»: одинаковые на вид
    числа, разный смысл;
  · «ВСЕГО | 120,00 кг | 48 | | 1 320 000,00» — строка-итог, которую наивный
    разбор превращает в ТОВАР с количеством 120 и ценой в 1,3 млн;
  · у одной шапки бывают ДВЕ колонки количества («Кол-во бл.» и «Кол-во шт.»);
  · цена в документе может быть без НДС, а сумма — с НДС: разделив одно на другое,
    получаешь количество, завышенное ровно на ставку налога.

Главный принцип разбора: **честно прочитанное важнее красиво посчитанного.**
Любое из трёх чисел строки можно ВЫЧИСЛИТЬ из двух других, и тогда самопроверка
«кол-во × цена = сумма» становится тавтологией — она сходится всегда и не значит
ничего. Поэтому строка помечается `read=True`, только если количество и сумма
физически напечатаны на ОДНОЙ строке документа, а метрика качества считает именно
такие строки.
"""
import re

from ..money import tolerance
from . import corpus

# ── ЛЕЙБЛЫ ИТОГА: пол в коде ∪ данные из doc_patterns ──────────────────────
# Асимметрия, которая когда-то стоила разбора целого документа: стоп-фильтр
# подвала знал слово «всего», а сборщик кандидатов итога — только три конкретные
# фразы. Когда кандидатов итога нет, судья-арифметика ВЫКЛЮЧАЕТСЯ, и побеждает
# самый тавтологичный разбор. Теперь список ОДИН и он — данные: новый лейбл
# нового поставщика или другой язык = INSERT в таблицу, а не правка этого файла.
_TOTAL_LABEL_FLOOR = ("итог", "всего", "к оплате", "jami", "жами", "hammasi",
                      "umumiy summa", "сумма к оплате")
_STOP_FLOOR = (r"итог", r"\bвсего\b", r"подпис", r"представит", r"получил")
_CACHE = {}


def patterns_reset():
    """Сбросить кэш (после `learn_pattern` и в оракулах)."""
    _CACHE.clear()


def _total_labels():
    if "total" not in _CACHE:
        vals = list(_TOTAL_LABEL_FLOOR)
        vals += [r["value"].lower() for r in corpus.patterns("invoice", "total_label")
                 if r["value"]]
        _CACHE["total"] = list(dict.fromkeys(v for v in vals if v))
    return _CACHE["total"]


def _total_label_rx():
    if "total_rx" not in _CACHE:
        _CACHE["total_rx"] = re.compile("|".join(re.escape(v) for v in _total_labels()), re.I)
    return _CACHE["total_rx"]


def _stop_rx():
    """Подвал документа режется у ВСЕХ семейств одинаково: стоп-слова + лейблы итога."""
    if "stop_rx" not in _CACHE:
        _CACHE["stop_rx"] = re.compile(
            "|".join(list(_STOP_FLOOR) + [re.escape(v) for v in _total_labels()]), re.I)
    return _CACHE["stop_rx"]


def _unit_words():
    if "units" not in _CACHE:
        vals = ["шт", "штук", "штука", "штуки", "дона", "блок", "блоков", "бл", "упак",
                "упаковка", "уп", "кор", "коробка", "пачка", "пачек", "кейс", "сум"]
        vals += [r["value"].lower() for r in corpus.patterns("invoice", "unit_alias")
                 if r["value"]]
        _CACHE["units"] = {v.strip(". ").lower().replace("ё", "е") for v in vals if v}
    return _CACHE["units"]


# ── ЧИСЛА И ДЕНЬГИ ─────────────────────────────────────────────────────────
_MONEY_MAX = 999_999_999
_MONEY_RE = re.compile(r"\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d{2})?|\d{4,}(?:[.,]\d{2})?")

# СКЛЕЙКА РАЗОРВАННЫХ ДЕНЕГ — ТОЛЬКО ЦЕЛЫМИ ЯЧЕЙКАМИ.
# Корень целого класса ошибок (десятки документов корпуса). Первая версия правила
# не требовала, чтобы левая часть была ЦЕЛОЙ ячейкой, и хватала КОПЕЕЧНЫЙ ХВОСТ
# предыдущего числа через границу «|»:
#   было  : … | 10 | 12 500.00 | 125 000 | 12% | 15 000.00 | 140 000.00
#   стало : … | 10 | 12 500.00125 000    | 12% | 15 000.00140 000.00
#   числа : [12500001, 25000, 15000001, 40000]  ← вместо [12500, 125000, 15000, 140000]
# Дальше парсер брал 1600 суммой и считал цену как сумма/количество — равенство
# становилось ТОЖДЕСТВЕННЫМ, строка отмечалась «сошлась» при 42 % потери денег.
# Теперь обе части обязаны быть целыми ячейками: исходное назначение («OCR разорвал
# разряд тысяч по соседним ячейкам») сохранено, склейка через копейки НЕВОЗМОЖНА.
_MONEY_JOIN_RE = re.compile(
    r"(?:(?<=\|)|^)[ \t]*(\d{1,3})[ \t]*\|[ \t]*(\d{3}(?:[.,]\d{2})?)[ \t]*(?=\||$)")


def join_split_money(s: str) -> str:
    """«| 70 | 308,00 |» → «|70308,00|». Только целые ячейки, см. комментарий выше."""
    return _MONEY_JOIN_RE.sub(r"\1\2", s or "")


_NUM_CELL_RE = re.compile(r"-?\d[\d  .,]*\d|-?\d")


def num_cell(c):
    """Текст ячейки → число со знаком.

    Дробная часть — последний разделитель, за которым НЕ РОВНО три цифры:
    «10.000000» → 10 (а не десять миллионов — живой класс, где количество
    печатается с шестью нулями после точки, цена при этом выходила 0, и строка
    считалась «зелёной» при количестве в миллион раз больше), «1 250 000.00» →
    1 250 000, «240 000,00» → 240 000, «12%» → 12.

    Знак «−» СОХРАНЯЕТСЯ: строка «финансовая скидка» с отрицательной суммой теряла
    минус, и Σ строк уезжала ровно на две скидки."""
    m = _NUM_CELL_RE.search((c or "").strip())
    if not m:
        return 0
    t = m.group(0).replace(" ", " ")
    neg = t.startswith("-")
    t = t.lstrip("-")
    frac = ""
    fm = re.search(r"[.,](\d+)$", t)
    if fm and len(fm.group(1)) != 3:
        frac, t = fm.group(1), t[:fm.start()]
    core = re.sub(r"[ .,]", "", t) or "0"
    if not core.isdigit():
        return 0
    val = float(core + ("." + frac if frac else ""))
    val = -val if neg else val
    return int(val) if float(val).is_integer() else round(val, 4)


def _row_tokens(s):
    """Числовые токены фрагмента строки ПО ПОРЯДКУ, как их напечатал документ.
    Есть «|» — токен = ЯЧЕЙКА (границы точные). Нет — режем позиционно: сперва
    денежные токены с разделителем разрядов, потом голые целые в промежутках
    (иначе «10 11 928.57» слиплось бы в одно число)."""
    s = s or ""
    if "|" in s:
        return [c.strip() for c in re.split(r"\s*\|\s*", s) if c.strip()]
    spans, covered = [], set()
    for m in _MONEY_RE.finditer(s):
        spans.append((m.start(), m.end(), m.group(0),
                      s[max(0, m.start() - 1):m.start()] == "-", s[m.end():m.end() + 1]))
        covered.update(range(m.start(), m.end()))
    for m in re.finditer(r"\d+(?:[.,]\d+)?", s):
        if m.start() in covered:
            continue
        spans.append((m.start(), m.end(), m.group(0), False, s[m.end():m.end() + 1]))
    return [("-" if neg else "") + t + ("%" if nxt == "%" else "")
            for _a, _b, t, neg, nxt in sorted(spans)]


def classify_num(t):
    """Токен → (kind, value): qty | money | junk; None — не число или ставка НДС.

    `qty` — «голое» число без разделителя разрядов и без копеек («60», «10.000000»):
    так документы печатают КОЛИЧЕСТВО. `money` — с разделителем разрядов или
    копейками. `junk` — длиннее девяти знаков: штрихкод 13 / код нацкаталога 17 /
    банковский счёт 20 — деньгами быть НЕ МОЖЕТ. Последнее не теория: без этого
    правила заявка на отгрузку ставила штрихкод 1 234 567 890 128 в цену, а
    прайс-лист отдавал 20-значный банковский счёт как «итог документа»."""
    t = (t or "").strip()
    if "%" in t or not re.search(r"\d", t):
        return None
    if len(re.sub(r"\D", "", t)) > 9:
        return "junk", num_cell(t)
    v = num_cell(t)
    grouped = bool(re.search(r"\d[ , .]\d{3}(?!\d)", t)) or bool(re.search(r"[.,]\d{1,2}$", t))
    if not grouped and float(v).is_integer() and 0 < abs(v) <= 9999:
        return "qty", v
    return "money", v


def row_numbers(s):
    """Числа фрагмента ПО ПОРЯДКУ: [(kind, value)]."""
    out = []
    for t in _row_tokens(s):
        c = classify_num(t)
        if c:
            out.append(c)
    return out


def _nums(s):
    """Крупные денежные токены как числа; всё длиннее девяти знаков отброшено
    ЗДЕСЬ — в единственной точке, откуда деньги берут все читатели."""
    out = []
    for x in _MONEY_RE.findall(s or ""):
        v = num_cell(x)
        if v and abs(v) <= _MONEY_MAX:
            out.append(v)
    return out


def _cells(s):
    return [c.strip() for c in re.split(r"\s*\|\s*", s or "") if c.strip()]


def _cells_pos(s):
    """Позиционный разбор pipe-строки: ПУСТЫЕ ЯЧЕЙКИ СОХРАНЯЮТСЯ.

    Разбор по шапке резолвит роли колонок ПО ИНДЕКСУ, поэтому индекс строки данных
    обязан позиционно совпадать с шапкой, а не «по счёту непустых ячеек». Живой
    корень бага: «Лимонад … | 10 | | 2 500,00 | 300 000,00» — пустая «Кол-во шт.»
    между «Кол-во бл.»=10 и ценой выпадала, и цена с суммой съезжали на колонку
    влево."""
    return [c.strip() for c in re.split(r"\s*\|\s*", s or "")]


def _small_int(cell):
    c = (cell or "").strip()
    return int(c) if re.fullmatch(r"\d{1,3}", c) else None


def _clean_name(s, keep_pack=False):
    s = re.sub(r"\b\d{8,17}\b", " ", s)                 # штрихкоды/длинные коды
    if not keep_pack:
        s = re.sub(r"\b\d+\s*[xх*]\s*\d+\b", " ", s)     # фасовка 1x6
    s = re.sub(r"\|+", " ", s)
    # Ведущий номер строки. ОТДЕЛИТЕЛЬ ОБЯЗАТЕЛЕН: пока он был необязательным,
    # срезалась и цифра, СЛИТНАЯ с именем («7Ветров Лимонад» → «Ветров Лимонад» — для
    # сопоставления с каталогом это уже другой товар).
    s = re.sub(r"^\s*\d{1,3}\s*[.)]\s*|^\s*\d{1,3}\s+", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" .,|-·\"'")
    return s[:90]


def _is_name_line(s):
    return len(re.findall(r"[A-Za-zА-Яа-яЁё]", s or "")) >= 4


# ── РУКОПИСНЫЕ ПОМЕТЫ ──────────────────────────────────────────────────────
# «8 000 (не привезли)» — продавец пишет от руки поверх напечатанной строки, сразу
# ПОСЛЕ цены. Первая версия детектора резала ЛЮБУЮ бесцифровую скобку — и печатные
# вкус/цвет «(зелёный)» массово стали «пометами», склеивая РАЗНЫЕ товары в одно имя
# (полсотни ложных срабатываний на корпусе). Теперь решает ПОЗИЦИЯ: скобка после
# денежного токена — помета, скобка внутри будущего имени — часть названия. Плюс
# третий рубеж ДАННЫМИ: фразы бланка («с фильтром», «упаковка») пометой не бывают
# никогда, и список этих фраз лежит в `doc_patterns`, а не здесь.
_NOTE_PAREN_RE = re.compile(r"\(([^()\d]{2,40})\)")
_NOTE_GAP_RX = re.compile(r"^[\s|]{0,6}$")


def _note_exclude():
    if "note_excl" not in _CACHE:
        vals = ["с фильтром", "упаковка"]
        vals += [r["value"].lower() for r in corpus.patterns("invoice", "note_exclude")
                 if r["value"]]
        _CACHE["note_excl"] = [v for v in dict.fromkeys(vals) if v]
    return _CACHE["note_excl"]


def note_from(block_text):
    """Рукописная помета анкерного блока (пусто — её нет)."""
    s = block_text or ""
    excl = _note_exclude()
    for m in _NOTE_PAREN_RE.finditer(s):
        phrase = m.group(1).strip(" .,-\"'")
        if not phrase or any(e in phrase.lower() for e in excl):
            continue
        before = s[:m.start()]
        mm = list(_MONEY_RE.finditer(before))
        if mm and _NOTE_GAP_RX.match(before[mm[-1].end():]):
            return phrase
    return ""


def strip_note(s):
    return _NOTE_PAREN_RE.sub(" ", s or "")


# ── ЯКОРЬ «ЯЧЕЙКА-ЕДИНИЦА» ─────────────────────────────────────────────────
_UNIT_CELL_RE = re.compile(
    r"^\s*(?:(-?\d+(?:[.,]\d+)?)\s*)?([A-Za-zА-Яа-яЁё'’]+\.?)\s*"
    r"(?:\(\s*([A-Za-zА-Яа-яЁё'’]+\.?)\s*\))?\s*$")


def _unit_cell(c):
    """Ячейка-ЕДИНИЦА → количество, вписанное в неё же (0 — единица без числа);
    None — это не единица. «шт», «шт. (упак)», «2 шт.», «дона»."""
    m = _UNIT_CELL_RE.match(c or "")
    if not m:
        return None
    units = _unit_words()
    w = (m.group(2) or "").strip(". ").lower().replace("ё", "е")
    w2 = (m.group(3) or "").strip(". ").lower().replace("ё", "е")
    if w not in units or (w2 and w2 not in units):
        return None
    return num_cell(m.group(1)) if m.group(1) else 0


def _unit_span(ln):
    """(начало, конец, количество-в-ячейке) якоря-единицы в СЫРОЙ строке.

    Ячейки бывают через «|» и через пробелы — один и тот же бланк один поставщик
    печатает и так, и так. Берём ПЕРВУЮ единицу, СПРАВА от которой есть минимум два
    числа и хотя бы одно из них деньги: так «блок» из расшифровки в конце строки
    якорем не становится, а название товара со словом-единицей внутри («Кофе в
    пачках») не режется — словарь единиц закрытый."""
    s = ln or ""
    if "|" in s:
        # есть ячейки — единицей может быть ТОЛЬКО ЦЕЛАЯ ячейка (иначе «200мл»
        # внутри имени товара становится якорем и режет название)
        spans = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[^|]+", s)]
    else:
        spans = [(m.start(1), m.end(1), m.group(1)) for m in re.finditer(
            r"(?:(?<=\s)|^)\s*((?:-?\d+(?:[.,]\d+)?\s*)?[A-Za-zА-Яа-яЁё'’]+\.?"
            r"(?:\s*\([A-Za-zА-Яа-яЁё'’]+\.?\))?)\s*(?=\s|$)", s)]
    for a, b, cell in spans:
        qty = _unit_cell(cell)
        if qty is None:
            continue
        nums = row_numbers(s[b:])
        if len(nums) >= 2 and any(k == "money" for k, _v in nums):
            return a, b, qty
    return None


_ROWNO_LEAD_RE = re.compile(r"^\s*(\d{1,3})[\s|]")
_ROWNO_RE = re.compile(r"^№?\s*\d{1,3}$")
_HDR_NAME = re.compile(r"наименован|номенклатур|товар|тмц|продукц|описание|назван", re.I)
_HDR_QTY = re.compile(r"кол-?во|количеств|\bкол\b|\bк-?во\b|\bшт\b", re.I)
_HDR_QTY_PACK = re.compile(r"\bбл\b|блок|\bуп\b|упак|кейс|короб", re.I)
_HDR_QTY_PIECE = re.compile(r"\bшт\b", re.I)
_HDR_PRICE = re.compile(r"цена", re.I)
_HDR_SUM = re.compile(r"стоимост|сумма", re.I)
_HDR_CODE = re.compile(r"артикул|штрих|^код$", re.I)
_HDR_VAT = re.compile(r"уч[её]т\w*\s*ндс|с\s+ндс", re.I)
# «Кол-во» строки-итога иногда несёт ВЕС в килограммах, а не штуки: второй рубеж
# защиты на случай подытога без явного слова «всего»/«итого» в тексте.
_QTY_WEIGHT_RE = re.compile(r"^\d+(?:[.,]\d+)?\s*кг\.?$", re.I)


def _is_header_line(s):
    """Строка-ШАПКА таблицы (≥2 словарных заголовка) — не товар и не имя товара."""
    low = (s or "").lower()
    hits = sum(1 for rx in (_HDR_NAME, _HDR_QTY, _HDR_PRICE, _HDR_SUM, _HDR_CODE)
               if rx.search(low))
    return hits >= 2


def _vat_rate(ln):
    """Ставка НДС строки: «12%», «12 %», «Без НДС»."""
    m = re.search(r"(\d{1,2})\s*%", ln or "")
    if m:
        return int(m.group(1))
    if re.search(r"без\s*ндс", ln or "", re.I):
        return 0
    return None


def _vat_tokens():
    if "vat_tok" not in _CACHE:
        vals = ["0", "12", "15", "20"]
        vals += [str(r["value"]).strip() for r in corpus.patterns("invoice", "vat_rate_token")
                 if r["value"]]
        _CACHE["vat_tok"] = {v for v in vals if v.isdigit()}
    return _CACHE["vat_tok"]


def _vat_rate_bare(ln, doc_has_vat):
    """Ставка ЦЕЛОЙ ЯЧЕЙКОЙ без знака «%» — так печатает часть бланков. Требуем,
    чтобы слово «НДС» вообще было в документе, иначе ячейка «12» (количество!)
    стала бы ставкой на бланке без налога. Список допустимых ставок — данные."""
    r = _vat_rate(ln)
    if r is not None or not doc_has_vat:
        return r
    toks = _vat_tokens()
    for c in _cells(ln):
        if c.strip() in toks:
            return int(c.strip())
    return None


def _find_pair(qty, monies):
    """ПЕРВАЯ пара напечатанных чисел строки, на которой замыкается арифметика
    «кол-во × цена = сумма» при этом количестве; None — не замыкается ни на одной.

    ОДИН перебор на два вопроса: «прочитано или выведено делением» (`_has_pair`) и
    «какая именно пара — цена и сумма» (`_vat_pair`). Пока переборов было два, они
    держали один и тот же допуск двумя копиями — то есть ждали, когда правка
    заденет одну и не заденет другую."""
    m = [v for v in (monies or []) if v]
    if not qty:
        return None
    for a in range(len(m)):
        for b in range(a + 1, len(m)):
            if abs(qty * m[a] - m[b]) <= tolerance.pair_eps(m[b]):
                return m[a], m[b]
    return None


def _has_pair(qty, monies):
    """Замыкается ли арифметика строки на ДВУХ НАПЕЧАТАННЫХ числах при этом
    количестве. Отличает «прочитано» от «выведено делением»: без этой проверки
    любое количество выглядит верным, потому что цену всегда можно поделить."""
    return _find_pair(qty, monies) is not None


def _vat_pair(qty, monies, rate):
    """(цена, сумма) ОДНОЙ НДС-ПЛОСКОСТИ.

    Класс ошибки: `qty = сумма_С_НДС / цена_БЕЗ_НДС` даёт систематическое ×1,12 на
    ВСЕХ строках документа (10→11, 20→22, 30→34) — на складе появляется товар,
    которого не привозили. Правило: сперва ищем ПАРУ, на которой арифметика
    замыкается по ПРОЧИТАННОМУ количеству, и только потом поднимаемся в плоскость
    «с НДС» (цена дилера из накладной с НДС — закон приёмки), причём поднимаемся
    ТЕМ ЖЕ множителем (1+ставка), а не берём вслепую последнюю сумму строки.

    Сравнение по МОДУЛЮ: у строки-скидки все суммы отрицательные, и «больше по
    величине» = −42 500 против −37 946 — знак терять нельзя."""
    money = [v for v in monies if v]
    if not money:
        return 0, 0
    price = summ = 0
    pair = _find_pair(qty, money)          # тот же перебор, что у `_has_pair`
    if pair:
        price, summ = pair
    if not price:
        summ = money[-1]
        price = round(summ / qty, 2) if qty else 0
        return price, summ
    k = 1 + (rate or 0) / 100.0
    withvat = next((v for v in reversed(money)
                    if abs(v) > abs(summ) + 1
                    and (not rate or abs(abs(v) - abs(summ) * k) <= max(2.0, abs(v) * 0.01))), 0)
    if withvat:
        summ = withvat
        price = round(summ / qty, 2) if qty else price
    return price, summ


def _name_near(rows, i, head_text):
    """Имя товара: из части строки ДО единицы, иначе из ближайшей строки выше или
    ниже. «Ниже» — не симметрии ради: у части бланков строка чисел печатается
    РАНЬШЕ имени, и без взгляда вниз четыре документа поставщика не читались вовсе."""
    if not _is_header_line(head_text):
        for c in (_cells(head_text) if "|" in (head_text or "") else [head_text]):
            cc = c.strip()
            if _ROWNO_RE.fullmatch(cc) or re.fullmatch(r"[\d ,.]+", cc):
                continue
            nm = _clean_name(cc, keep_pack=True)
            if _is_name_line(nm):
                return nm
    for j in list(range(i - 1, max(-1, i - 3), -1)) + list(range(i + 1, min(len(rows), i + 3))):
        s = rows[j]
        if not s.strip() or _is_header_line(s) or _stop_rx().search(s):
            continue
        if _ROWNO_RE.fullmatch(s.strip()):
            continue
        cand = _clean_name(s, keep_pack=True)
        if _is_name_line(cand):
            return cand
    return ""


# ── ПОСТОБРАБОТКА СТРОКИ ───────────────────────────────────────────────────
def apply_pack_size(x):
    """БЛОКИ ВМЕСТО ШТУК.

    Бумага печатает: «Кол-во бл.» = 10, «Кол-во шт.» ПУСТА, цена 2 500 — за ШТУКУ,
    сумма 300 000. Наивный разбор кладёт количество 10 и цену 30 000 за блок: на
    склад приходит 10 единиц вместо 120, а закупочный профиль видит цену в
    двенадцать раз выше реальной. Правило: если арифметика строки НЕ замыкается на
    напечатанном количестве, но `сумма / цена` — целое и кратно ему, то
    напечатанное — БЛОКИ, единиц = сумма/цена, а частное — фасовка.

    Считается ОДНИМ местом для всех семейств: второй арифметики не заводим. Когда
    количество × цена = сумма, правило не срабатывает вовсе."""
    q, p, s = x.get("qty") or 0, x.get("price") or 0, x.get("sum") or 0
    if not (q > 0 and p > 0 and s > 0) or x.get("pack_qty"):
        return x
    if abs(q * p - s) <= tolerance.row_eps(s):
        return x                                   # арифметика уже сошлась
    pieces = s / p
    if abs(pieces - round(pieces)) > 0.01:
        return x
    pieces = int(round(pieces))
    if pieces <= q or pieces % int(q):
        return x
    x["pack_qty"], x["qty"] = q, pieces
    x["pack_size"] = pieces // int(q)
    return x


def line_ok(x):
    """САМОПРОВЕРКА строки «кол-во × цена = сумма» — ОДНА формула на весь проект.

    ⚠ Она СЛАБАЯ по построению (см. `line_read` ниже): у строки, где хоть одно
    число вычислено, она истинна всегда. Отдельной функцией живёт именно поэтому:
    мутационный контроль (`recognition/golden.py`) обязан пересчитывать `ok` ТОЙ
    ЖЕ формулой, что и парсер. Пока формула была скопирована туда вручную,
    доказательство «слабая метрика диверсию не видит» держалось на том, что две
    копии не разошлись, — а это ровно тот дубль, который в проекте блокер."""
    q, p, s = x.get("qty") or 0, x.get("price") or 0, x.get("sum") or 0
    return bool(q > 0 and (not (p and s) or abs(q * p - s) <= tolerance.row_eps(s)))


def _finish(x):
    if not x.get("sum") and x.get("qty") and x.get("price"):
        x["sum"] = x["qty"] * x["price"]
    apply_pack_size(x)
    x["ok"] = line_ok(x)
    x.setdefault("note_handwritten", "")
    x.setdefault("code", "")
    return x


# ── РАЗБОР ПО ШАПКЕ КОЛОНОК ────────────────────────────────────────────────
def parse_header_table(text):
    """Чистая pipe-таблица: шапка Наименование/Кол-во/Цена/Стоимость → разбор ПО
    КОЛОНКАМ. [] если шапки нет — тогда решают другие кандидаты.

    Если шапка несёт ОБЕ колонки количества («Кол-во бл.» + «Кол-во шт.») — читаем
    ОБЕ: «шт.» приоритет для единиц, «бл.» — фасовочная информация строки. Раньше
    колонка резолвилась первым совпадением слева направо, и «бл.» всегда побеждала
    просто по порядку колонок, даже когда реальное число единиц стояло правее."""
    rows = (text or "").splitlines()
    hi = ncol = qcol = packcol = pcol = scol = ccol = vcol = None
    for i, ln in enumerate(rows):
        if "|" not in ln:
            continue
        cells = _cells_pos(ln)
        if len(cells) < 4:
            continue
        nc = next((k for k, c in enumerate(cells) if _HDR_NAME.search(c)), None)
        qty_cols = [k for k, c in enumerate(cells) if _HDR_QTY.search(c)]
        pc = next((k for k, c in enumerate(cells) if _HDR_PRICE.search(c)), None)
        sc = next((k for k, c in enumerate(cells) if _HDR_SUM.search(c)), None)
        if nc is not None and qty_cols and (pc is not None or sc is not None):
            qc_piece = next((k for k in qty_cols if _HDR_QTY_PIECE.search(cells[k])), None)
            qc_pack = next((k for k in qty_cols if _HDR_QTY_PACK.search(cells[k])), None)
            if qc_piece is not None and qc_pack is not None:
                qc, packcol = qc_piece, qc_pack
            else:
                qc, packcol = qty_cols[0], None
            hi, ncol, qcol, pcol, scol = i, nc, qc, pc, sc
            ccol = next((k for k, c in enumerate(cells) if _HDR_CODE.search(c)), None)
            # колонка «итог строки С НДС» — последняя из совпавших. НЕ «цена с НДС»
            # (это цена за штуку, её делить на количество нельзя).
            vcol = next((k for k in range(len(cells) - 1, -1, -1)
                         if _HDR_VAT.search(cells[k]) and "цена" not in cells[k].lower()), None)
            break
    if hi is None:
        return []
    lines = []
    for ln in rows[hi + 1:]:
        if _stop_rx().search(ln):
            break
        cells = _cells_pos(ln)
        if "|" not in ln or len(cells) <= max(ncol, qcol):
            continue
        if all(_small_int(c) is not None for c in cells):
            continue                       # строка-подпись колонок «1 | 2 | 3 …»
        qty_texts = [cells[qcol].strip()]
        if packcol is not None and packcol < len(cells):
            qty_texts.append(cells[packcol].strip())
        if any(_QTY_WEIGHT_RE.match(t) for t in qty_texts if t):
            continue                       # подытог с весом в килограммах
        note = note_from(ln)
        name = _clean_name(strip_note(cells[ncol]) if note else cells[ncol], keep_pack=True)
        if not _is_name_line(name):
            continue
        qty_piece = num_cell(cells[qcol])
        pack_qty = num_cell(cells[packcol]) if (packcol is not None
                                                and packcol < len(cells)) else 0
        qty = qty_piece or pack_qty        # «шт» приоритет, «бл» — фолбэк на пустой ячейке
        price = num_cell(cells[pcol]) if (pcol is not None and pcol < len(cells)) else 0
        summ = num_cell(cells[scol]) if (scol is not None and scol < len(cells)) else 0
        if vcol is not None and vcol < len(cells) and qty:
            vat_total = num_cell(cells[vcol])
            if vat_total:
                price, summ = round(vat_total / qty), vat_total
        code = ""
        if ccol is not None and ccol < len(cells):
            mc = re.search(r"\d{4,}", cells[ccol])
            code = mc.group(0) if mc else ""
        if qty and qty > 0:
            x = {"name": name, "code": code, "qty": int(qty) if float(qty).is_integer() else qty,
                 "price": price, "sum": summ, "note_handwritten": note}
            if packcol is not None and pack_qty and pack_qty != qty:
                x["pack_qty"] = int(pack_qty)
            lines.append(x)
    return [_finish(x) for x in lines]


# ── РАЗБОР ПО ЯКОРЮ-ЕДИНИЦЕ ────────────────────────────────────────────────
def parse_unit_anchor(text):
    """Универсальный разбор для бланков, где у строки есть ячейка-единица.

    Судья — арифметика в ОДНОЙ НДС-плоскости; количество берётся ПРОЧИТАННЫМ (голое
    целое рядом с единицей либо число внутри ячейки «2 шт.»), а не выводится
    делением, пока это возможно. [] — бланк не этого вида."""
    rows = (text or "").splitlines()
    doc_has_vat = bool(re.search(r"ндс|qqs", text or "", re.I))
    lines = []
    for i, raw in enumerate(rows):
        ln = join_split_money(raw)
        if _stop_rx().search(ln) or _is_header_line(ln):
            continue
        sp = _unit_span(ln)
        if sp is None:
            continue
        a, b, qty_in_cell = sp
        head, tail = ln[:a], ln[b:]
        # №пп ведущим числом ЛИБО код первой ячейкой ЛИБО ячейки слева от единицы —
        # иначе это свободный текст с единицей (подпись, примечание в подвале).
        if not (_ROWNO_LEAD_RE.match(ln) or re.match(r"^\s*\d{4,6}\s*[\s|]", ln)
                or "|" in ln[:a] or (a == 0 and ln.count("|") >= 3)):
            continue
        nums = row_numbers(tail)
        # деньгами может оказаться и «голое» четырёхзначное число: различить
        # количество от цены по написанию нельзя, поэтому судьёй остаётся арифметика,
        # а в кандидаты идут оба вида.
        monies = [v for k, v in nums if k == "money" or (k == "qty" and abs(v) >= 1000)]
        if not monies:
            continue
        rate = _vat_rate_bare(ln, doc_has_vat)
        # КАНДИДАТЫ КОЛИЧЕСТВА по приоритету: число внутри ячейки-единицы → голое
        # целое СПРАВА → голое целое СЛЕВА. Правильный выбирает АРИФМЕТИКА. Урок:
        # у бланка бывает ВТОРАЯ колонка количества «в кейсе» — это фасовка, и
        # якорь цеплял её, отдавая 1 при реальных 24.
        cand_q, seen = [], set()
        for v in ([qty_in_cell] if qty_in_cell else []) \
                + [v for k, v in nums if k == "qty"] \
                + [v for k, v in row_numbers(head) if k == "qty"]:
            if v and v not in seen:
                seen.add(v)
                cand_q.append(v)
        qty = price = summ = 0
        for cq in cand_q:
            if _has_pair(cq, monies):
                qty, (price, summ) = cq, _vat_pair(cq, monies, rate)
                break
        if not qty:
            qty = cand_q[0] if cand_q else 0
            price, summ = _vat_pair(qty, monies, rate)
        if not qty and summ and price:
            qty = max(1, round(summ / price))
        if not (qty and summ):
            continue
        note = note_from(ln)
        name = _name_near(rows, i, strip_note(head) if note else head)
        if not name:
            continue
        code = ""
        mc = re.match(r"^\s*(\d{4,7})\s+(\S.*)$", name)
        if mc:
            code, name = mc.group(1), mc.group(2).strip()
        lines.append({"name": name, "code": code,
                      "qty": int(qty) if float(qty).is_integer() else qty,
                      "price": price, "sum": summ, "note_handwritten": note,
                      "vat_rate": rate})
    return [_finish(x) for x in lines]


# ── РАЗБОР ПО СТРУКТУРЕ (без опоры на слова шапки) ─────────────────────────
def _header_roles(text, width):
    """Индексы колонок «количество» и «цена» ИЗ ЗАГОЛОВКА.

    Нужен, потому что арифметика СИММЕТРИЧНА: `кол-во × цена = сумма` одинаково
    верно при любой из двух расстановок, и какая победит — решал бы порядок
    перебора. Заголовок — единственный честный различитель: у накладной порядок
    «Кол-во · Цена», у выгрузки из учётной системы обратный. По величине различать
    нельзя: 600 штук по 400 сум — обычная строка, количество больше цены."""
    for ln in (text or "").splitlines():
        low = ln.lower()
        if not (re.search(r"кол-?во|количеств|продано", low) and re.search(r"цена", low)):
            continue
        cells = _cells(ln) if "|" in ln else \
            [c.strip() for c in re.split(r"\s{2,}", ln.strip()) if c.strip()]
        if len(cells) != width:
            continue
        qi = pi = None
        for k, c in enumerate(cells):
            cl = c.lower()
            if qi is None and re.search(r"кол-?во|количеств|продано", cl) \
                    and not re.search(r"кейс|упак|короб", cl):
                qi = k
            elif pi is None and "цена" in cl:
                pi = k
        if qi is not None and pi is not None:
            return qi, pi
    return None, None


def parse_grid_table(text):
    """Таблица товаров ПО СТРУКТУРЕ, без опоры на слова.

    Живой случай: колонки назывались «ТМЦ» и «Кол (шт)» — ни один словарь не подошёл,
    и чистая печатная накладная стала для бота невидимой. Здесь заголовки не нужны:
    берём строки с одинаковым числом ячеек, где ровно одна колонка текстовая (имя
    товара), а среди остальных есть количество и деньги. Правильность проверяем
    арифметикой на ВСЕХ строках сразу."""
    rows = []
    for ln in (text or "").splitlines():
        if _stop_rx().search(ln):
            continue
        cells = _cells(ln) if "|" in ln else \
            [c.strip() for c in re.split(r"\s{2,}", ln.strip()) if c.strip()]
        if len(cells) < 3:
            continue
        rows.append(cells)
    if not rows:
        return []
    width = max({len(r) for r in rows}, key=lambda w: sum(1 for r in rows if len(r) == w))
    body = [r for r in rows if len(r) == width]
    if len(body) < 2:
        return []

    def _txt(c):
        return bool(_is_name_line(_clean_name(c, keep_pack=True)))

    ncol = next((k for k in range(width)
                 if sum(1 for r in body if _txt(r[k])) >= max(1, len(body) - 1)), None)
    if ncol is None:
        return []
    nums = [k for k in range(width) if k != ncol
            and sum(1 for r in body if num_cell(r[k])) >= max(1, len(body) - 1)]
    if len(nums) < 2:
        return []
    best = None
    for qi in nums:
        for pi in nums:
            for si in nums:
                if len({qi, pi, si}) != 3:
                    continue
                hits = 0
                for r in body:
                    qv, pv, sv = num_cell(r[qi]), num_cell(r[pi]), num_cell(r[si])
                    if qv and pv and sv and abs(qv * pv - sv) <= 1:
                        hits += 1
                if hits and (best is None or hits > best[0]):
                    best = (hits, qi, pi, si)
    if not best or best[0] < max(1, len(body) - 1):
        return []
    _h, qcol, pcol, scol = best
    hq, hp = _header_roles(text, width)
    if hq is not None and hp is not None and {hq, hp} == {qcol, pcol}:
        qcol, pcol = hq, hp
    lines = []
    for r in body:
        raw_row = " | ".join(r)
        note = note_from(raw_row)
        name = _clean_name(strip_note(r[ncol]) if note else r[ncol], keep_pack=True)
        qty = num_cell(r[qcol])
        if not (_is_name_line(name) and qty and qty > 0):
            continue                      # шапка и строка-итог сюда не попадут
        lines.append({"name": name, "code": "",
                      "qty": int(qty) if float(qty).is_integer() else qty,
                      "price": num_cell(r[pcol]), "sum": num_cell(r[scol]),
                      "note_handwritten": note})
    return [_finish(x) for x in lines]


# ── ИТОГ ДОКУМЕНТА ─────────────────────────────────────────────────────────
_WORD_NUM = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
}
_WORD_MUL = {"тысяч": 1000, "тысяча": 1000, "тысячи": 1000, "миллион": 1000000,
             "миллиона": 1000000, "миллионов": 1000000, "миллиард": 1000000000}


def total_words(text):
    """Сумма ПРОПИСЬЮ после лейбла итога → int (0, если её нет).
    «Шестьсот десять тысяч сум» → 610 000.

    Не экзотика: у части документов корпуса это ЕДИНСТВЕННЫЙ печатный итог, и без
    него судья-арифметика выключалась — а выключенный судья означает победу самого
    тавтологичного разбора."""
    rows = (text or "").splitlines()
    rx = _total_label_rx()
    for i, ln in enumerate(rows):
        if not rx.search(ln.lower()):
            continue
        window = " ".join(rows[i:i + 3]).lower().replace("ё", "е")
        window = re.split(r"в\s*т\.?\s*ч\.?|в\s+том\s+числе", window, maxsplit=1)[0]
        total = part = 0
        seen = False
        for w in re.findall(r"[а-я]+", window):
            if w in _WORD_NUM:
                part += _WORD_NUM[w]
                seen = True
            elif w in _WORD_MUL:
                total += max(1, part) * _WORD_MUL[w]
                part = 0
                seen = True
            elif seen and w.startswith(("сум", "тийин", "so")):
                break        # терминатор ТОЛЬКО после числительных: иначе сам лейбл
                             # «Всего отпущено на СУММУ:» обрывал бы разбор
        if seen and (total + part) >= 1000:
            return total + part
    return 0


_INN_LABEL_RE = re.compile(r"инн|р/с|р\\с|счет|счёт|мфо|тел|phone|карта|номер", re.I)


def doc_nonmoney(text):
    """ПОЗИЦИОННОЕ правило: числа, которые в ЭТОМ документе деньгами быть не могут —
    ИНН, расчётный счёт, МФО, телефон ИЗ ШАПКИ (первые 20 строк, рядом с лейблом) и
    любой 13-значный штрихкод. Живые случаи: строка «Комментарий … +998 XX XXX XX
    XX» становилась товаром с ценой 90 000; «итогом документа» становился ИНН
    магазина — ровно девять знаков, ограничение длины он проходит."""
    bad = set()
    rows = (text or "").splitlines()
    for ln in rows[:20]:
        if not _INN_LABEL_RE.search(ln):
            continue
        for m in re.finditer(r"\d[\d  ]{6,}\d", ln):
            d = re.sub(r"\D", "", m.group(0))
            if 9 <= len(d) <= 20:
                bad.add(int(d))
    for m in re.finditer(r"\b\d{13}\b", text or ""):
        bad.add(int(m.group(0)))
    return bad


def total_candidates(text):
    """ВСЕ кандидаты на «итог документа».

    В строке «Итого» счёта-фактуры стоят ТРИ суммы — стоимость без НДС, сам НДС и
    стоимость с НДС. Строки товаров считаются с НДС, поэтому брать максимум окна
    нельзя: выигрывала сумма без налога и рисовалась ложная красная плашка
    расхождения на ВЕРНОМ приходе. Отдаём все кандидаты, выбирает `total()` — тот,
    что сошёлся с Σ строк.

    Кандидаты собираются со ВСЕХ строк-итогов, а не с первой: у части бланков их
    три подряд («сумма без переоценки» · «сумма переоценки» · «итого»)."""
    rows = (text or "").splitlines()
    out = []
    bad = doc_nonmoney(text)
    for i, ln in enumerate(rows):
        if not _total_label_rx().search(ln.lower()):
            continue
        window = " ".join(rows[i:i + 3])
        # режем «в т.ч. НДС …» — там сумма налога, а не итог
        window = re.split(r"в\s*т\.?\s*ч\.?|в\s+том\s+числе|вкл\.?\s*ндс",
                          window, maxsplit=1, flags=re.I)[0]
        # в pipe-таблице «|» — ГРАНИЦА ЯЧЕЙКИ, и склейка разрядов через неё ломала
        # числа («1 200 000,00 | 144 000,00» → 1 200 000 001)
        if "|" in window:
            n = [v for c in _cells(window) for v in _nums(c)]
        else:
            n = _nums(join_split_money(window))
        out += [v for v in n if abs(v) <= _MONEY_MAX and v not in bad]
        if len(out) >= 8:
            break
    return list(dict.fromkeys(out))


def total(text, lines_sum=None):
    """Итог документа: кандидат, сошедшийся с Σ строк (это и есть «с учётом НДС»),
    иначе максимум окна. Итог ПРОПИСЬЮ — такой же полноправный кандидат: у части
    бланков он единственный печатный."""
    n = list(total_candidates(text))
    tw = total_words(text)
    if tw and tw not in n:
        n.append(tw)
    if not n:
        return None
    if lines_sum:
        hit = next((v for v in n if abs(v - lines_sum) <= 1), None)
        if hit:
            return hit
    return max(n)


# ── ЧЕСТНОСТЬ ЧТЕНИЯ И ИНВАРИАНТЫ ──────────────────────────────────────────
def row_value_sets(text):
    """Числа КАЖДОЙ физической строки документа — основа честной метрики
    «прочитано, а не выведено»."""
    out = []
    for ln in (text or "").splitlines():
        vals = set()
        for _k, v in row_numbers(join_split_money(ln)):
            vals.add(int(v))
            vals.add(round(float(v), 2))
        out.append(vals)
    return out


def line_read(x, rowvals):
    """Строка ЧЕСТНАЯ: количество (или напечатанное блочное `pack_qty`) И сумма
    стоят на ОДНОЙ физической строке документа. Цена законно выводится из
    суммы-с-НДС, поэтому в судью не входит.

    ⚠ Это ключевая метрика проекта. Поле `ok` («кол-во × цена = сумма») выполняется
    ПО ПОСТРОЕНИЮ у всех разборов, которые хоть одно число вычисляют. На рабочем
    корпусе среди строк с `ok=True` честно прочитанных оказалось МЕНЬШЕ ТРЕТИ —
    29 %. Пока качество мерили через `ok`, порча парсера УЛУЧШАЛА метрику."""
    q, s = x.get("qty") or 0, x.get("sum") or 0
    if not (q and s):
        return False
    pq = x.get("pack_qty") or 0
    for vals in rowvals:
        if int(s) not in vals and round(float(s), 2) not in vals:
            continue
        if int(q) in vals or (pq and int(pq) in vals):
            return True
    return False


def max_rowno(text):
    """Максимальный НЕПРЕРЫВНЫЙ № п/п документа — скелет сверки числа строк.
    Осторожно: нумерация на фото обрезается, а транскрипция умеет продублировать
    номер, поэтому №пп только ПОДПИСЫВАЕТ расхождение, судьёй он НЕ является."""
    seen = set()
    for ln in (text or "").splitlines():
        m = _ROWNO_LEAD_RE.match(ln)
        if m and not _stop_rx().search(ln):
            seen.add(int(m.group(1)))
    n = 0
    while n + 1 in seen:
        n += 1
    return n


def invariants(text, lines, total_sum, rowvals=None):
    """Поля-инварианты документа + пометки строк. Ничего не «исправляет» молча —
    только ЧЕСТНО подписывает, что прочитано, что выведено и где не сходится."""
    rowvals = rowvals if rowvals is not None else row_value_sets(text)
    bad = doc_nonmoney(text)
    ok_read = ok_derived = junk = 0
    for x in lines:
        rd = line_read(x, rowvals)
        x["read"] = rd
        x["derived"] = not rd          # хоть одно число выведено → «не проверена»
        j = []
        for fld in ("price", "sum"):
            v = abs(int(x.get(fld) or 0))
            if v > _MONEY_MAX:
                j.append("%s: число длиннее 9 знаков" % fld)
            elif v and v in bad:
                j.append("%s: это ИНН/счёт/телефон/штрихкод из документа" % fld)
        x["junk"] = "; ".join(j)
        ok_read += 1 if rd else 0
        ok_derived += 0 if rd else 1
        junk += 1 if j else 0
    ls = sum(x.get("sum") or 0 for x in lines)
    diff = (total_sum - ls) if total_sum else 0
    # Расхождение «итог ≠ Σ строк» — ЖЁСТКИЙ повод: либо потеряна строка, либо
    # ошибка поставщика. Приход блокируется МЯГКО (плашка + просьба посмотреть),
    # а не запрещается: решает владелец.
    hard = bool(total_sum and not tolerance.covered(diff, len(lines), 0, hard=False))
    warn = []
    if hard:
        warn.append("итог документа %s ≠ Σ строк %s (%s%s) — не хватает строки или цифр"
                    % (int(total_sum), int(ls), "+" if diff > 0 else "", int(diff)))
    mx = max_rowno(text)
    if mx and len(lines) != mx:
        warn.append("№пп до %d, а строк разобрано %d" % (mx, len(lines)))
    if junk:
        warn.append("%d строк(и) с числом-не-деньгами (штрихкод/ИНН/телефон)" % junk)
    return {"ok_read": ok_read, "ok_derived": ok_derived, "junk_lines": junk,
            "max_rowno": mx, "total_diff": diff, "hard_mismatch": hard,
            "needs_owner": bool(hard or junk), "warn": warn,
            "allowance": tolerance.allowance(len(lines))}


# ── ЭТО ВООБЩЕ НАКЛАДНАЯ? ──────────────────────────────────────────────────
_NOT_INVOICE_FLOOR = ("цена на полке", "рекомендованн", "прайс", "price list",
                      "информация о заявке", "кол-во в заявке")


def _not_invoice_markers():
    vals = list(_NOT_INVOICE_FLOOR)
    vals += [r["value"].lower() for r in corpus.patterns("invoice", "not_invoice_marker")
             if r["value"]]
    return list(dict.fromkeys(v for v in vals if v))


def not_invoice_reason(text, lines=None):
    """Почему это НЕ накладная (пусто — накладная).

    Прайс-лист поставщика и заявка на отгрузку попадали в приход как накладные:
    прайс давал 38 «товарных строк», заявка ставила штрихкод в цену. Различитель —
    по КОЛИЧЕСТВУ и ИТОГУ, а не «доля строк с ценой»: у настоящей накладной цена
    есть у ВСЕХ строк, поэтому такой признак работал ровно наоборот."""
    low = (text or "").lower()
    hit = next((v for v in _not_invoice_markers() if v in low), "")
    if hit:
        return "маркер не-накладной: «%s»" % hit
    if lines is not None:
        with_qty = sum(1 for x in lines if (x.get("qty") or 0) > 0)
        if lines and not with_qty and not total_candidates(text):
            return "нет ни одного количества и нет итога документа"
    return ""


# ── ШАПКА ДОКУМЕНТА ────────────────────────────────────────────────────────
def _supplier(text):
    for m in re.finditer(r"[Пп]оставщик[:\s]*([^\n|]{3,60})", text or ""):
        cand = m.group(1).strip(" \"'|")
        if cand:
            return cand[:60]
    return "?"


def _number(text):
    # Знак «№» распознавание отдаёт то как «№», то как «Nº»/«N°» — это КЛАСС ошибки
    # чтения, а не особенность одного бланка: на фото документов он стабильно
    # распадается на латинскую N и надстрочный знак, и номер накладной терялся
    # целиком вместе с дедупликацией по номеру.
    NUM = r"[N№][º°]?"
    # Алфанумерический номер («12АБ-004567») ищется ДО общей цифровой ветки: та
    # первой съест цифровой хвост, и до алфанумерики дело не дойдёт. Но засчитываем
    # его только при КОНТЕКСТНОМ ЯКОРЕ в 40 символах перед ним — иначе номер
    # договора или партии тихо подменял бы номер накладной, ломая дедупликацию.
    anchor = re.compile(r"№|\bN\b|НАКЛАДН|СЧЕТ|СЧЁТ", re.I)
    for m in re.finditer(r"\b(\d{2}[А-ЯA-Z]{2}-\d{4,10})\b", text or ""):
        if anchor.search((text or "")[max(0, m.start() - 40):m.start()]):
            return m.group(1)
    m = re.search(r"(?:СЧЕТ[-\s]*ФАКТУР|НАКЛАДН)\w*[^\d]{0,40}" + NUM + r"\s*(\d{4,12})",
                  text or "", re.I)
    if m:
        return m.group(1)
    m = re.search(NUM + r"\s*(\d{4,12})", text or "")
    return m.group(1) if m else ""


def _date(text):
    m = re.search(r"[Дд]ата[:\s]*(\d{2})[.\s](\d{2})[.\s](\d{4})", text or "")
    if m:
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    m = re.search(r"\b(\d{2})[.](\d{2})[.](\d{4})\b", text or "")
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else ""


# ── ДИСПЕТЧЕР ──────────────────────────────────────────────────────────────
def parse_invoice(text: str):
    """Дословный текст накладной → разбор или None.

    ТАЙ-БРЕЙК — ГЛАВНОЕ МЕСТО ЭТОЙ ФУНКЦИИ. Кандидатов разбора несколько (по шапке,
    по якорю-единице, по структуре), и выбрать надо ОДИН.

    Судья первой очереди — печатный ИТОГ документа: чей Σ строк с ним сошёлся, тот
    и прав. Допуск судьи СТРОГИЙ (полтора сума, не процент): ослабление до 0,1 %
    проверено замером и ЛОМАЕТ выбор — обрезанный разбор (три строки из четырёх,
    минус 380 сум) «сходится» с итогом и побеждает полный.

    Судья второй очереди — число ЧЕСТНО ПРОЧИТАННЫХ строк (`line_read`), а НЕ число
    строк с `ok=True`. Старый судья считал `ok`, и разбор, который вычисляет цену из
    суммы, систематически побеждал честный: на живом документе он отдавал 60 млн
    вместо 3 млн — и все строки были «зелёные»."""
    if not (text or "").strip():
        return None
    rowvals = row_value_sets(text)
    unit = parse_unit_anchor(text)
    hdr = parse_header_table(text)
    grid = parse_grid_table(text)

    cands = total_candidates(text)
    tw = total_words(text)
    if tw:
        cands = list(dict.fromkeys(list(cands) + [tw]))

    def _sum(ls):
        return sum(x.get("sum") or 0 for x in ls)

    def _fits(ls):
        return bool(ls) and any(abs(_sum(ls) - c) <= tolerance.judge_eps() for c in cands)

    def _read(ls):
        return sum(1 for x in ls if line_read(x, rowvals))

    order = [("hdr", hdr), ("unit", unit), ("grid", grid)]
    family, lines = next(((n, ls) for n, ls in order if _fits(ls)), ("", None))
    if lines is None:
        best = max(range(len(order)), key=lambda i: (_read(order[i][1]),
                                                     len(order[i][1]), -i))
        family, lines = order[best][0], order[best][1] or hdr or unit or grid
    if not lines:
        return None
    reason = not_invoice_reason(text, lines)
    if reason:
        return None                       # прайс/заявка — приходовать нечего
    ls = _sum(lines)
    tot = total(text, ls)
    # помета вне анкерных блоков строк — общий комментарий ДОКУМЕНТА, не строки
    line_notes = {x["note_handwritten"] for x in lines if x.get("note_handwritten")}
    excl = _note_exclude()
    doc_notes = [m.group(1).strip(" .,-\"'") for m in _NOTE_PAREN_RE.finditer(text or "")
                 if m.group(1).strip(" .,-\"'")
                 and not any(e in m.group(1).lower() for e in excl)]
    out = {"family": family, "supplier": _supplier(text), "number": _number(text),
           "date": _date(text), "lines": lines, "total_sum": tot, "lines_sum": ls,
           "doc_comment": "; ".join(dict.fromkeys(n for n in doc_notes
                                                  if n not in line_notes)),
           "ok_lines": sum(1 for x in lines if x.get("ok")), "ok": True}
    out.update(invariants(text, lines, tot, rowvals))
    return out
