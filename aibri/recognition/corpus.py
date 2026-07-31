"""ЕДИНАЯ БАЗА ЗНАНИЙ О ДОКУМЕНТАХ: корпус + паттерны.

Принцип владельца дословно: НЕ отдельные распознавалки под каждый тип бумаги —
ОДНА база знаний на все документы, всё взаимосвязано и обучаемо.

  `doc_corpus`   — файл ↔ полный разбор ↔ вид документа ↔ кто разобрал
                   (patterns / api / owner_edit) ↔ уверенность. Копится КАЖДЫМ
                   разбором из любого источника: бот становится точнее без правок
                   кода.
  `doc_patterns` — лейблы, варианты написаний, маркеры секций, форматы полей,
                   привязанные к виду документа. Новый лейбл нового поставщика —
                   СТРОКА В ТАБЛИЦУ, а не правка .py.

ЗАКОН L-17 «правку владельца стереть нельзя». `forget()` когда-то делал
`DELETE FROM doc_corpus`. При этом правки владельца живут в корпусе как
`source='owner_edit'`, и из них же растёт golden-корпус эталонов — то есть
перевешивание ОДНОГО алиаса физически уничтожало историю решений человека.
Теперь строки помечаются снятыми (`state='withdrawn'`), а `owner_edit` перед
снятием зачисляются в golden. Читатели не изменились ни на бит: `corpus_for`
отдаёт только актуальные. Стереть правку владельца кодом теперь нельзя никаким
путём — и это правильная асимметрия.

Копией-ТАБЛИЦЕЙ golden сознательно не делается: второе хранилище тех же данных
пришлось бы синхронизировать, и оно бы разошлось (ПРАВИЛО ДУБЛЯ — один смысл,
одна реализация).
"""
import json
import re

from ..db import conn, q

# ── СИД: правила данными, а не кодом ───────────────────────────────────────
# Пол (константы в коде) нужен, чтобы парсер работал и без базы — в оракулах и на
# первом старте. Всё, что сверх пола, живёт строками в `doc_patterns`.
_INVOICE_SEED = (
    ("total_label", "label:total", "итого"),
    ("total_label", "label:total", "всего к оплате"),
    ("total_label", "label:total", "жами"),          # узбекская латиница/кириллица
    ("total_label", "label:total", "jami"),
    ("unit_alias", "unit:piece", "шт"),
    ("unit_alias", "unit:piece", "дона"),
    ("unit_alias", "unit:pack", "упак"),
    ("unit_alias", "unit:pack", "кор"),
    ("col_alias", "col:qty", "кол-во"),
    ("col_alias", "col:price", "цена"),
    ("col_alias", "col:sum", "сумма"),
    ("vat_rate_token", "vat:rate", "12"),
    ("vat_rate_token", "vat:rate", "0"),
    ("not_invoice_marker", "marker:pricelist", "цена на полке"),
    ("not_invoice_marker", "marker:pricelist", "прайс-лист"),
    ("not_invoice_marker", "marker:request", "информация о заявке"),
    ("note_exclude", "phrase:blank", "с фильтром"),
    ("note_exclude", "phrase:blank", "упаковка"),
)
_ZREPORT_SEED = (
    ("marker", "section:tolovlar", "TO'LOVLAR"),
    ("marker", "section:qaytaruvlar", "QAYTARUVLAR"),
    ("marker", "section:jami", "JAMI"),
    ("label_alias", "field:z_no", "Z-hisobot raqami"),
    ("label_alias", "field:opened", "Ochilish sanasi"),
    ("label_alias", "field:closed", "Yopilish sanasi"),
    ("label_alias", "field:cash", "Umumiy naqd pul miqdori"),
    ("label_alias", "field:card", "Umumiy karta miqdori"),
    ("label_alias", "field:total", "Umumiy summa"),
    ("label_alias", "field:vat", "Umumiy QQS miqdori"),
    # Аппараты и каналы витрины УСЛОВНЫЕ: важна не платёжная система, а сам факт,
    # что привязка «канал → аппарат» живёт СТРОКОЙ В ТАБЛИЦЕ, а не в коде.
    ("terminal", "terminal:1", "Аппарат №1 · канал-A/канал-B/канал-C"),
    ("terminal", "terminal:2", "Аппарат №2 · канал-D"),
)


def seed():
    """Идемпотентно (UNIQUE по четвёрке) — безопасно звать на каждый старт."""
    with conn() as c:
        for kind, rows in (("invoice", _INVOICE_SEED), ("zreport", _ZREPORT_SEED)):
            for pattern_kind, key, value in rows:
                c.execute(
                    "INSERT INTO doc_patterns(doc_kind,pattern_kind,key,value,weight,created) "
                    "VALUES(?,?,?,?,1,datetime('now','localtime')) "
                    "ON CONFLICT(doc_kind,pattern_kind,key,value) DO NOTHING",
                    (kind, pattern_kind, key, value))


def patterns(doc_kind, pattern_kind):
    try:
        return q("SELECT key, value, weight FROM doc_patterns "
                 "WHERE doc_kind=? AND pattern_kind=? ORDER BY weight DESC, id",
                 (doc_kind, pattern_kind))
    except Exception:            # noqa: BLE001 — без базы остаётся константный пол
        return []


def learn_pattern(doc_kind, pattern_kind, key, value, weight=1):
    """Новое написание лейбла/маркера — ДАННЫМИ. Идемпотентно: повтор не плодит строк."""
    if not (doc_kind and pattern_kind and key and value):
        return False
    with conn() as c:
        cur = c.execute(
            "INSERT INTO doc_patterns(doc_kind,pattern_kind,key,value,weight,created) "
            "VALUES(?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(doc_kind,pattern_kind,key,value) DO NOTHING",
            (doc_kind, pattern_kind, key, str(value), weight))
        return cur.rowcount > 0


# ── корпус ─────────────────────────────────────────────────────────────────
def record(file_path, doc_kind, parsed, source, confidence):
    """Записать разбор. Реплей (тот же файл+вид+источник дал ТОТ ЖЕ результат)
    строку не дублирует. Сравнение идёт с последней АКТУАЛЬНОЙ строкой: снятая
    история совпадением не считается, иначе повторное обучение тому же значению
    после un-teach молча не записалось бы."""
    payload = json.dumps(parsed or {}, ensure_ascii=False, sort_keys=True)
    with conn() as c:
        last = c.execute(
            "SELECT parsed_json FROM doc_corpus WHERE file_path=? AND doc_kind=? "
            "AND source=? AND COALESCE(state,'live')='live' ORDER BY id DESC LIMIT 1",
            (file_path or "", doc_kind or "", source)).fetchone()
        if last and last["parsed_json"] == payload:
            return False
        c.execute(
            "INSERT INTO doc_corpus(file_path,doc_kind,parsed_json,source,confidence,created) "
            "VALUES(?,?,?,?,?,datetime('now','localtime'))",
            (file_path or "", doc_kind or "", payload, source, float(confidence or 0)))
        return True


def forget(file_path, doc_kind):
    """L-17: снять строки ключа, НЕ удаляя. `owner_edit` перед снятием зачисляются
    в golden — история решений владельца не короче, чем была."""
    with conn() as c:
        c.execute("UPDATE doc_corpus SET golden=1 WHERE file_path=? AND doc_kind=? "
                  "AND source='owner_edit' AND COALESCE(state,'live')='live'",
                  (file_path or "", doc_kind or ""))
        cur = c.execute("UPDATE doc_corpus SET state='withdrawn',"
                        "withdrawn=datetime('now','localtime') "
                        "WHERE file_path=? AND doc_kind=? AND COALESCE(state,'live')='live'",
                        (file_path or "", doc_kind or ""))
        return cur.rowcount > 0


def corpus_for(file_path, doc_kind=None):
    """Актуальные строки ключа (снятые `forget`-ом не отдаются)."""
    sql = "SELECT * FROM doc_corpus WHERE file_path=? AND COALESCE(state,'live')='live'"
    args = [file_path or ""]
    if doc_kind:
        sql += " AND doc_kind=?"
        args.append(doc_kind)
    return q(sql + " ORDER BY id DESC", tuple(args))


def owner_history(file_path, doc_kind=None):
    """ВСЯ история ключа, включая снятое — сырьё генератора эталонов."""
    sql = "SELECT * FROM doc_corpus WHERE file_path=?"
    args = [file_path or ""]
    if doc_kind:
        sql += " AND doc_kind=?"
        args.append(doc_kind)
    return q(sql + " ORDER BY id", tuple(args))


def golden_rows(doc_kind=None):
    """Правки владельца, зачисленные в golden ИЛИ ещё живые."""
    sql = ("SELECT * FROM doc_corpus WHERE source='owner_edit' "
           "AND (COALESCE(golden,0)=1 OR COALESCE(state,'live')='live')")
    args = []
    if doc_kind:
        sql += " AND doc_kind=?"
        args.append(doc_kind)
    return q(sql + " ORDER BY id", tuple(args))


# ── СВЁРТКА OCR: классы ошибок вместо словаря опечаток ─────────────────────
# Термопечать чека даёт устойчивые КЛАССЫ ошибок, а не случайный шум: «ll»
# читается как «n», «rn» как «m», кириллические близнецы подменяют латиницу,
# «q»↔«g», «2»↔«z». Держать по строке в таблице на каждую опечатку бессмысленно
# (их бесконечно много) — поэтому свёртка снимает КЛАСС, а разумные написания
# лейблов по-прежнему добавляются данными.
_FOLD_DIGRAPHS = (("ll", "n"), ("rn", "m"))
_FOLD_CYR = {"о": "o", "с": "c", "е": "e", "а": "a", "р": "p", "у": "y", "х": "x",
             "к": "k", "м": "m", "т": "t", "в": "b", "н": "h", "і": "i", "ѕ": "s"}
_FOLD_CLS = {"q": "g", "ı": "i", "í": "i", "ï": "i", "y": "v", "j": "i", "2": "z"}


def ocr_fold(text):
    """(свёрнутый текст, начала, концы). Свёртка: диграфы → кириллические близнецы
    → классы ошибок → выброс всего неалфавитного (пробелы, «|», «:», апострофы —
    OCR ставит их где попало). `ends[i]` = индекс в ОРИГИНАЛЕ сразу за i-м
    свёрнутым символом; по нему значение поля читается из исходного текста."""
    s = text or ""
    out, ends, starts = [], [], []
    i, n = 0, len(s)
    while i < n:
        low = s[i].lower()
        pair = low + (s[i + 1].lower() if i + 1 < n else "")
        digraph = next((b for a, b in _FOLD_DIGRAPHS if pair == a), None)
        if digraph:
            out.append(digraph)
            starts.append(i)
            ends.append(i + 2)
            i += 2
            continue
        c = _FOLD_CLS.get(_FOLD_CYR.get(low, low), _FOLD_CYR.get(low, low))
        if c.isalnum():
            out.append(c)
            starts.append(i)
            ends.append(i + 1)
        i += 1
    return "".join(out), starts, ends


def label_hits(text, label, fuzz=None, prefix=None):
    """Все вхождения лейбла ПО СВЁРТКЕ — (start, end) в ОРИГИНАЛЬНОМ тексте, точные
    совпадения первыми. Пустой лейбл → [] (не матчим всё подряд).

    `fuzz` — сколько свёрнутых знаков разрешено НЕ совпасть; по умолчанию одна
    ошибка на восемь знаков (лейбл короче восьми — ноль допуска, иначе короткое
    слово матчило бы полтекста). `prefix` — сколько первых свёрнутых знаков считать
    достаточными: ХВОСТ лейбла самое битое место, он стоит вплотную к числу, и OCR
    портит его ВСТАВКОЙ знака, чего допуск-замена не берёт.

    Ошибочный матч не опасен: значение всё равно проверяется арифметикой документа,
    а не принимается на веру."""
    ft, starts, ends = ocr_fold(text)
    fl = ocr_fold(label)[0]
    if prefix:
        fl = fl[:int(prefix)]
    if not fl or not ft:
        return []
    out, at = [], ft.find(fl)
    while at >= 0:
        out.append((starts[at], ends[at + len(fl) - 1]))
        at = ft.find(fl, at + 1)
    tol = len(fl) // 8 if fuzz is None else int(fuzz)
    if not tol or len(fl) > len(ft):
        return out
    exact = {p for p, _ in out}
    for i in range(len(ft) - len(fl) + 1):
        if starts[i] in exact:
            continue
        bad = 0
        for j, ch in enumerate(fl):
            if ft[i + j] != ch:
                bad += 1
                if bad > tol:
                    break
        if bad <= tol:
            out.append((starts[i], ends[i + len(fl) - 1]))
    return out


def sections(text, doc_kind="zreport"):
    """Границы секций документа из `doc_patterns` (pattern_kind='marker').
    Маркер, напечатанный на бланке КАПСОМ, сравнивается с учётом регистра: иначе
    заголовок секции «JAMI» ловил бы слово «Jami» внутри строки поля из шапки и
    рвал разбор блоков."""
    marks = []
    for row in patterns(doc_kind, "marker"):
        name = row["key"].split(":", 1)[-1]
        upper = row["value"].isupper()
        for start, end in label_hits(text, row["value"]):
            if upper and not text[start:end].isupper():
                continue
            marks.append((start, name))
    marks.sort()
    return marks


def section_at(pos, sects):
    cur = "head"
    for p, name in sects:
        if p <= pos:
            cur = name
        else:
            break
    return cur


# ЧТЕНИЕ ЗНАЧЕНИЯ ПОЛЯ — ОДИН ДОМ НА ПРОЕКТ.
# Разбор Z-отчёта держал свою копию этой пары (регекс + разбор числа), и копии уже
# разъехались: одна допускала ПЕРЕНОС СТРОКИ внутри числа, другая нет. Осталась та,
# что живёт на бумаге: разделитель разрядов — только ПРОБЕЛ. Перенос строки
# отделителем разрядов быть НЕ может, потому что на широком кадре (Z и четыре слипа
# одним снимком) следующая строка принадлежит СОСЕДНЕЙ бумаге — окно, шагнувшее
# через перенос, утащило бы чужое число. Хвост, реально разорванный переносом,
# достраивается отдельно и только с подтверждением арифметикой (`zreport._tail_join`).
_NUM_RE = re.compile(r"[:=|\s);.,]{0,6}(\d+(?:[ ]\d{3}(?!\d))*(?:[.,]\d{1,2}(?!\d))?)")


def to_num(s):
    """Текст числа с бумаги → int/float (None — не число). Пробелы и «|» внутри
    выбрасываются: распознавание ставит их где попало. Запятая = десятичный знак."""
    s = re.sub(r"[\s|]", "", s or "").replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return int(v) if float(v).is_integer() else v


def num_after(text, pos, window=40):
    """Число СРАЗУ за позицией. Между лейблом и значением бывает не только «:» и
    «|»: свёртка обрезает лейбл по последней буквенно-цифровой букве, поэтому
    закрывающая скобка или точка с запятой остаются ПЕРЕД числом."""
    m = _NUM_RE.match(text[pos:pos + window])
    return to_num(m.group(1)) if m else None


def num_near(text, pos, window=45):
    """Число В ОКНЕ после лейбла (не обязательно вплотную): OCR ставит между
    подписью и значением мусор — «Umumiy summa; | 2 500 000», точка с запятой
    вместо двоеточия. Кандидат всё равно проходит через арифметику документа,
    поэтому окно тут не опасно."""
    m = _NUM_RE.search(text[pos:pos + window])
    return to_num(m.group(1)) if m else None
