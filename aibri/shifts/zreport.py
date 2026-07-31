"""ПАРСЕР ФИСКАЛЬНЫХ Z-ОТЧЁТОВ (кассовая смена).

Работники магазина раз в смену фотографируют пачку чеков и кидают в рабочий чат.
На фото ГЛАВНЫЙ — фискальный Z-отчёт на узбекской латинице (Z-hisobot raqami ·
Ochilish/Yopilish sanasi · CHEKLAR · TO'LOVLAR · QAYTARUVLAR · JAMI), рядом —
слипы эквайринга и отчёт закрытия смены терминала.

Канон распознавания: внешняя модель, если её вообще зовут, отдаёт ТОЛЬКО дословную
транскрипцию текста. ВСЕ правила разбора и сверки — детерминированные, здесь.
Бесплатный локальный OCR (macOS Vision через `ocrmac`) стоит СЛОЕМ ПЕРЕД платным
вызовом: уверен — платный вызов не происходит вовсе.

ЭТО ДЕНЬГИ: парсер НИЧЕГО не подгоняет. Не сошлось — честный warning и
`confident=False`, а не «поправим, чтобы красиво».

ДВА ЖИВЫХ КЛАССА ОШИБОК, ради которых написана половина файла:

1. **Число, разорванное переносом строки.** Термочек узкий, «350 000.00»
   печатается как «350» и ниже «000.00». Достраиваем ТОЛЬКО если недостающий хвост
   однозначно выводится из двух других проверенных чисел и прочитанный обрывок —
   его ПРЕФИКС.

2. **Число, разорванное соседней колонкой.** 97 % фото — широкий кадр, где Z и
   четыре слипа сняты одним снимком, и OCR сшивает строки СОСЕДНИХ бумаг в одну
   ленту через « | »: «Umumiy naqd pul miqdori: | 700 | Комиссия | 0,00» — сумма
   700 000.00 разорвана, её хвост стоит на следующей строке среди чужих колонок.
   Здесь достройка из двух других чисел не спасает: на широком кадре рвутся сразу
   два. Поэтому собираем ВСЕ ЧТЕНИЯ каждого поля (строгий регекс по секциям +
   терпимый поиск лейбла по свёртке OCR + склейка хвоста с соседней строки) и
   ВЫБИРАЕМ ТРОЙКУ, которая СХОДИТСЯ АРИФМЕТИЧЕСКИ: нал + карта = итог.

Ничего не выдумывается: каждый кандидат — цифры, реально напечатанные на бумаге;
арифметика документа лишь ОТСЕИВАЕТ обрывки.
"""
import os
import re

from ..recognition import corpus

# Геометрия страницы и локальный OCR — не свойство смены, а свойство БУМАГИ, и
# живут они в `recognition/ocr.py`. Здесь они только ПЕРЕИМПОРТИРОВАНЫ: дверь для
# вызывающих (`zreport.recognizer_ready()`) осталась той же, дом переехал.
from ..recognition.ocr import LOCAL_OCR_NOTE, ocr_image, recognizer_ready  # noqa: F401

_NOT_Z_MARKERS = ("NOT_Z_REPORT", "NOT_ZREPORT")
# Канонический маркер в коде был один, а агенты писали в кэш-файлы второе
# написание — дюжина файлов «не Z» распозналась бы как Z. Спасал порог «≥3
# маркера», но это ловушка: описательный текст слипа, где случайно наберётся три
# Z-слова, был бы разобран как фискальный отчёт. Теперь оба написания — маркер.

_L_ZNO = re.compile(r"z\s*-?\s*hisobot\s+raqami", re.I)
_L_FM = re.compile(r"\bFM\s+raqami", re.I)
_L_OPEN = re.compile(r"ochilish\s+sanas", re.I)
_L_CLOSE = re.compile(r"yopilish\s+sanas", re.I)
_L_CHECKS = re.compile(r"\bcheklar\b", re.I)
_L_CASH = re.compile(r"umumiy\s+naqd", re.I)
_L_CARD = re.compile(r"umumiy\s+karta", re.I)
_L_TOTAL = re.compile(r"umumiy\s+summa", re.I)
_L_VAT = re.compile(r"umumiy\s+qqs", re.I)

_DT_RE = re.compile(r"(\d{4})[-./](\d{2})[-./](\d{2})[ T]+(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?")
_TAIL_TOKEN_RE = re.compile(r"(?:^|\|)\s*(\d{3}(?:[.,]\d{2})?)\s*(?:\||$)", re.M)

_Z_MONEY_LABELS = {
    "cash": ("Umumiy naqd pul miqdori",),
    "card": ("Umumiy karta miqdori",),
    "total": ("Umumiy summa",),
    "vat": ("Umumiy QQS miqdori",),
}
_Z_DATE_LABELS = {"opened": ("Ochilish sanasi",), "closed": ("Yopilish sanasi",)}


# ── распознавание фото: бесплатный слой первым ─────────────────────────────
# ПЛАТФОРМА. Сам распознаватель и геометрия страницы переехали в
# `recognition/ocr.py` (это про бумагу, а не про смену) и переимпортированы выше —
# `zreport.recognizer_ready()` и `zreport.ocr_image()` продолжают работать. Здесь
# осталось то, что действительно про СМЕНУ: кэш-спутник рядом с фотографией и
# порядок слоёв «кэш → бесплатный OCR → (платный вызов)».
def cached_text(path):
    """Транскрипт из файла-спутника `.ztxt` БЕЗ похода куда-либо. Распознаём фото
    ОДИН раз, инвалидация по времени изменения оригинала."""
    if not path:
        return None
    side = str(path) + ".ztxt"
    try:
        if os.path.exists(side) and (not os.path.exists(path)
                                     or os.path.getmtime(side) >= os.path.getmtime(path)):
            with open(side, encoding="utf-8") as f:
                return f.read().strip() or None
    except OSError:
        pass
    return None


def transcribe(path, free_only=True):
    """Текст Z-отчёта: кэш-спутник → локальный OCR. `free_only=True` — платный
    внешний вызов НЕ делается НИКОГДА.

    Отдельный режим существует не для красоты: пока фоновый процесс ходил только в
    кэш и возвращался ДО локального слоя, бесплатное распознавание было
    недостижимо из фона, и весь архив смен разбирался вручную."""
    t = cached_text(path)
    if t:
        return t, "cache"
    text, _err = ocr_image(path)
    if text:
        return text, "ocr"
    if free_only:
        return None, "unavailable"
    return None, "needs_paid_api"


# ── лейблы: пол в коде ∪ данные ────────────────────────────────────────────
def _labels(field, floor=()):
    out = list(floor)
    for r in corpus.patterns("zreport", "label_alias"):
        if r["key"] == f"field:{field}" and r["value"] not in out:
            out.append(r["value"])
    return out


_FOLD_FIELDS = ("z_no", "opened", "closed", "cash", "card", "total")


def is_zreport(text):
    """Фискальный Z-отчёт? Маркер «не Z» проверяется ДО порога — см. комментарий
    к `_NOT_Z_MARKERS`.

    Два прохода. Строгий — как печатает исправный чек. Если строгих признаков мало,
    считаем поля, найденные ПО СВЁРТКЕ OCR: на широком кадре термопечать бьёт
    подписи полей так, что строгих совпадений остаётся два из семи, и документ
    объявлялся «не Z-отчётом» вместе со всей выручкой смены."""
    t = text or ""
    if any(m in t.upper() for m in _NOT_Z_MARKERS):
        return False
    marks = sum(bool(rx.search(t)) for rx in
                (_L_ZNO, _L_FM, _L_TOTAL, _L_CASH, _L_CARD, _L_OPEN, _L_CHECKS))
    if marks >= 3:
        return True
    folded = 0
    for field in _FOLD_FIELDS:
        floor = _Z_MONEY_LABELS.get(field) or _Z_DATE_LABELS.get(field) \
            or (("Z-hisobot raqami",) if field == "z_no" else ())
        if any(corpus.label_hits(t, lb) for lb in _labels(field, floor)):
            folded += 1
    return folded >= 3


def _dt_from(m):
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    return f"{y}-{mo}-{d} {int(hh):02d}:{mm}:{ss or '00'}"


def _date_field(text, field, strict_rx):
    """Дата смены С БУМАГИ. Сначала строгий регекс, затем ТЕРПИМЫЙ путь: лейбл
    ищется по свёртке OCR.

    Зачем: живая смена погибла на ОДНОЙ букве — распознавание прочло «Ochilish
    sallasi» вместо «sanasi», строгий шаблон не сработал, поле осталось пустым, и
    смена выпала из сопоставления ЦЕЛИКОМ вместе с выручкой. Свёртка «ll»→«n» лечит
    весь класс, а новое написание лейбла добавляется СТРОКОЙ в таблицу паттернов,
    не правкой этого файла.

    Значение читается ТОЛЬКО ДО КОНЦА СТРОКИ: без этой границы окно перешагивало
    перенос и утаскивало ЧУЖУЮ дату с соседней бумаги в кадре."""
    m = strict_rx.search(text or "")
    if m:
        v = _dt_from(_DT_RE.search(text[m.end():m.end() + 60]))
        if v:
            return v
    for prefix in (None, 8):
        for label in _labels(field, _Z_DATE_LABELS.get(field, ())):
            for _s, end in corpus.label_hits(text, label, prefix=prefix):
                seg = text[end:end + 90].split("\n", 1)[0]
                v = _dt_from(_DT_RE.search(seg))
                if v:
                    return v
    return None


def _z_no(text):
    """Номер Z-отчёта: строгий шаблон, затем поиск лейбла по свёртке OCR.

    Живой класс: на семи фото из восьми термопечать читалась как «2-hisobot ragami»
    вместо «Z-hisobot raqami» — ОБЯЗАТЕЛЬНОЕ поле не находилось вовсе, и смена не
    попадала в отчёты. Свёртка «2»→«z» и «q»→«g» лечит весь класс сразу, а не
    одно написание."""
    m = _L_ZNO.search(text or "")
    if m:
        v = corpus.num_after(text, m.end())
        if v is not None:
            return v
    for label in _labels("z_no", ("Z-hisobot raqami",)):
        for _s, end in corpus.label_hits(text, label):
            v = corpus.num_after(text, end)
            if v is not None:
                return v
    return None


def _tail_join(t, pos, value):
    """Кандидат-склейка: обрывок числа (≤3 знаков, без копеек) + хвост-токен
    «NNN[.NN]» с одной из следующих строк того же кадра."""
    if value is None or value != int(value) or int(value) <= 0 or int(value) > 999:
        return None
    seg = t[pos:pos + 260]
    nl = seg.find("\n")
    if nl < 0:
        return None
    m = _TAIL_TOKEN_RE.search(seg[nl:])
    if not m:
        return None
    joined = corpus.to_num(str(int(value)) + m.group(1))
    return joined if joined and joined > value else None


def _money_candidates(t, field, strict_rx, sects):
    """ВСЕ чтения денежного поля: [(значение, секция)]. Три источника — строгий
    регекс по секциям, терпимый поиск лейбла по свёртке, склейка разорванного
    хвоста. Порядок доверия: сначала секция платежей, потом остальные."""
    out = []
    for m in strict_rx.finditer(t):
        v = corpus.num_after(t, m.end())
        if v is not None:
            sec = corpus.section_at(m.start(), sects)
            out.append((v, sec))
            j = _tail_join(t, m.end(), v)
            if j is not None:
                out.append((j, sec))
    for label in _labels(field, _Z_MONEY_LABELS.get(field, ())):
        hits = list(corpus.label_hits(t, label))
        hits += [h for h in corpus.label_hits(t, label, prefix=9) if h not in hits]
        for s, end in hits:
            for v in (corpus.num_after(t, end), corpus.num_near(t, end)):
                if v is None:
                    continue
                sec = corpus.section_at(s, sects)
                if (v, sec) not in out:
                    out.append((v, sec))
                j = _tail_join(t, end, v)
                if j is not None and (j, sec) not in out:
                    out.append((j, sec))
    prefer = ("tolovlar", "jami")
    rank = {name: i for i, name in enumerate(prefer)}
    out.sort(key=lambda vs: (rank.get(vs[1], len(prefer)), -vs[0]))
    return out


def _prefix_ok(part, whole):
    a, b = str(int(round(part))), str(int(round(whole)))
    return len(b) > len(a) and b.startswith(a)


def solve_money(cands, primary):
    """Тройка (нал, карта, итог), которая СХОДИТСЯ (допуск 1 сум).

    Уже сходится ПЕРВИЧНОЕ чтение (обычный случай) — возвращаем None, поведение не
    меняется ни на копейку. Иначе:
      шаг 1 — перебор ЧТЕНИЙ: тройка, сходящаяся арифметически, с НАИБОЛЬШИМ итогом
              (обрывок числа всегда меньше целого);
      шаг 2 — тройки нет: пара сходится сама с собой, а третье число ДОСТРАИВАЕТСЯ
              и принимается ТОЛЬКО если прочитанный обрывок — его ПРЕФИКС. Иначе
              None, и парсер честно скажет «не прочитаны все суммы»: выдумывать
              число, не подтверждённое бумагой, нельзя."""
    pc, pk, pt = primary
    if None not in primary and abs(pc + pk - pt) <= 1:
        return None
    best = None
    for c in cands["cash"]:
        for k in cands["card"]:
            for tt in cands["total"]:
                if abs(c[0] + k[0] - tt[0]) <= 1 and tt[0] > 0:
                    if best is None or tt[0] > best[2]:
                        best = (c[0], k[0], tt[0])
    if best:
        return best
    for k in cands["card"]:
        for tt in cands["total"]:
            if tt[0] <= 0 or k[0] <= 0 or k[0] >= tt[0]:
                continue
            cand = tt[0] - k[0]
            for c in cands["cash"]:
                if c[0] > 0 and _prefix_ok(c[0], cand):
                    if best is None or tt[0] > best[2]:
                        best = (cand, k[0], tt[0])
    if best:
        return best
    for c in cands["cash"]:
        for tt in cands["total"]:
            if tt[0] <= 0 or c[0] <= 0 or c[0] >= tt[0]:
                continue
            cand = tt[0] - c[0]
            for k in cands["card"]:
                if k[0] > 0 and _prefix_ok(k[0], cand):
                    if best is None or tt[0] > best[2]:
                        best = (c[0], cand, tt[0])
    return best


def parse_text(text):
    """Дословный текст Z-отчёта → словарь смены или None.

    `confident=False` означает «прочитано не всё» — такую смену апп в отчёты не
    берёт и честно ставит в очередь к человеку, а не показывает как факт."""
    if not is_zreport(text):
        return None
    t = text or ""
    sects = corpus.sections(t, "zreport")
    z_no = _z_no(t)
    m = _L_FM.search(t)
    fm_no = None
    if m:
        mm = re.search(r"[:=|\s]{0,6}([A-Za-z0-9\-]{4,20})", t[m.end():m.end() + 40])
        fm_no = mm.group(1) if mm else None
    opened = _date_field(t, "opened", _L_OPEN)
    closed = _date_field(t, "closed", _L_CLOSE)

    cands = {f: _money_candidates(t, f, rx, sects) for f, rx in
             (("cash", _L_CASH), ("card", _L_CARD), ("total", _L_TOTAL), ("vat", _L_VAT))}

    def _primary(field):
        for v, sec in cands[field]:
            if sec in ("tolovlar", "head"):
                return v
        return cands[field][0][0] if cands[field] else None

    cash, card, total, vat = (_primary(f) for f in ("cash", "card", "total", "vat"))
    fixed = solve_money(cands, (cash, card, total))
    repaired = bool(fixed)
    if fixed:
        cash, card, total = fixed

    warn = []
    key_ok = all(v is not None for v in (z_no, opened, closed, cash, card, total))
    sum_ok = key_ok and abs(cash + card - total) <= 1
    if key_ok and not sum_ok:
        warn.append(f"нал+карта ({cash}+{card}) ≠ итог ({total})")
    if not key_ok:
        warn.append("прочитаны не все обязательные поля Z-отчёта")
    if opened and closed and closed < opened:
        warn.append("закрытие раньше открытия")
    return {"z_no": int(z_no) if z_no is not None else None, "fm_no": fm_no,
            "opened": opened, "closed": closed,
            "cash": int(round(cash)) if cash is not None else None,
            "card": int(round(card)) if card is not None else None,
            "total": int(round(total)) if total is not None else None,
            "vat": int(round(vat)) if vat is not None else None,
            "repaired": repaired, "warnings": warn,
            "confident": bool(key_ok and sum_ok and not warn)}
