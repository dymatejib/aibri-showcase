"""ФОТО-ПУТЬ: фотография → локальный OCR → разбор → эталон бумаги.

Три JPEG в `fixtures/photos/` — одна и та же демо-накладная, снятая тремя
способами: ровно, под углом ~8° и с бликом. Бумага на них НАРИСОВАНА кодом,
настоящая только съёмка — наклон, перспектива, блик, замятость, зерно, JPEG. Это
не «happy path»: под углом Vision отдаёт фрагменты, которые наивная группировка
сшивает поперёк строк, а блик съедает часть шапки.

ПЛАТФОРМЫ. Бесплатный локальный OCR — это macOS Vision через пакет `ocrmac`, и он
существует только на macOS. Поэтому файл делится на две части:

  · КРОСС-ОС (гоняется везде): фотографии на месте, это валидные JPEG,
    эталон фото совпадает с эталоном текстовой фикстуры, а модуль распознавания
    БЕЗ `ocrmac` отвечает ошибкой, а не падает импортом;
  · macOS (иначе skip с причиной): настоящий прогон Vision по трём фото.

Проверка «файл — валидный JPEG» сделана стандартной библиотекой: у витрины нет
обязательных зависимостей, и заводить Pillow ради трёх байт заголовка мы не
станем. Если Pillow в окружении уже есть (он приезжает вместе с `ocrmac`), тот же
файл дополнительно открывается им.
"""
import json
import math
import os
import struct
import sys

import pytest

from aibri.recognition import golden as G
from aibri.recognition import ocr as O
from aibri.recognition import paper_invoice as pi
from aibri.shifts import zreport as Z

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHOTOS = os.path.join(ROOT, "fixtures", "photos")

with open(os.path.join(PHOTOS, "paper.json"), encoding="utf-8") as _f:
    PAPER = json.load(_f)
DOC = PAPER["doc"]
SHOTS = [(s["file"], s["note"]) for s in PAPER["shots"]]
IDS = [s[0][:-4] for s in SHOTS]
NEED_LINES = math.ceil(len(DOC["lines"]) * PAPER["tolerance"]["min_lines_ratio"])

# Один ответ на весь проект: доступен ли бесплатный локальный распознаватель.
# Сравнивать `sys.platform` здесь нельзя — на маке без установленного `ocrmac`
# ответ такой же «нет», и тест обязан пропуститься, а не покраснеть.
mac_only = pytest.mark.skipif(not O.recognizer_ready(), reason=O.LOCAL_OCR_NOTE)


@pytest.fixture(autouse=True)
def _db(seeded_db):
    """Лейблы итога и единицы приходят ДАННЫМИ — разбору нужна база."""
    return seeded_db


def jpeg_size(path):
    """(ширина, высота) JPEG по заголовку — стандартной библиотекой, без Pillow.
    Заодно это и проверка целостности: у обрезанного файла кадр кончится раньше
    маркера размера, и функция скажет об этом, а не вернёт мусор."""
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            raise ValueError("не JPEG: нет маркера начала кадра")
        while True:
            b = f.read(1)
            while b and b != b"\xff":                 # ищем начало маркера
                b = f.read(1)
            marker = f.read(1)
            while marker == b"\xff":                  # заполнитель между маркерами
                marker = f.read(1)
            if not marker:
                raise ValueError("файл кончился раньше размеров кадра")
            m = marker[0]
            if m == 0xD9 or m == 0x01 or 0xD0 <= m <= 0xD7:
                continue                              # маркеры без тела
            head = f.read(2)
            if len(head) < 2:
                raise ValueError("файл кончился на заголовке сегмента")
            (length,) = struct.unpack(">H", head)
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                body = f.read(length - 2)             # SOF: точность, высота, ширина
                if len(body) < 5:
                    raise ValueError("обрезанный сегмент размеров")
                h, w = struct.unpack(">HH", body[1:5])
                return w, h
            f.seek(length - 2, 1)


# ── кросс-ОС ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,note", SHOTS, ids=IDS)
def test_фото_на_месте_и_это_валидный_jpeg(name, note):
    path = os.path.join(PHOTOS, name)
    assert os.path.exists(path), f"нет фикстуры {name} ({note})"
    w, h = jpeg_size(path)
    assert w > 800 and h > 600, f"{name}: {w}×{h} — для OCR это уже не документ"
    assert os.path.getsize(path) > 20_000, f"{name}: файл подозрительно пуст"


def test_фото_открывается_и_через_pil_если_он_есть():
    """Pillow не обязателен: он приезжает с `ocrmac` на маке, а на Linux/Windows
    ставится отдельно. Есть — проверяем его глазами тоже."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow не установлен — целостность JPEG проверена заголовком")
    for name, _note in SHOTS:
        with Image.open(os.path.join(PHOTOS, name)) as im:
            im.verify()                              # битый файл здесь и упадёт


def test_эталон_фото_совпадает_с_текстовой_фикстурой():
    """ОДНА БУМАГА — ДВА КАНАЛА ЧТЕНИЯ. Строки фото повторяют строки текстовой
    фикстуры `inv_01_header_pack`, и это проверяется машиной: разъедься эталоны,
    фото-путь начал бы «подтверждать» не тот документ."""
    txt = next(d for d in G.load_golden()["docs"] if d["key"] == DOC["same_as"])
    keys = ("name", "qty", "price", "sum")
    assert [tuple(x[k] for k in keys) for x in DOC["lines"]] == \
           [tuple(x[k] for k in keys) for x in txt["lines"]]
    assert DOC["paper_total"] == txt["paper_total"]


def test_без_локального_ocr_модуль_отвечает_а_не_падает(monkeypatch):
    """СИМУЛЯЦИЯ НЕ-МАКА на любой ОС: пакета `ocrmac` в окружении нет.

    Импорты `aibri.recognition.ocr` и `aibri.shifts.zreport` уже состоялись — ни
    один не тянет `ocrmac` на уровне модуля, иначе Linux и Windows потеряли бы
    вместе с фото-путём весь текстовый разбор смен. А `ocr_image` обязан вернуть
    ОТВЕТ с причиной: вызывающий по этому ответу выбирает следующий слой."""
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    assert O.recognizer_ready() is False
    text, err = O.ocr_image(os.path.join(PHOTOS, SHOTS[0][0]))
    assert text is None
    assert err.startswith("ocrmac_missing") and "macOS" in err


def test_ответ_есть_ли_ocr_один_на_проект():
    """Дом распознавателя переехал в `recognition/ocr.py`, дверь осталась прежней:
    `zreport.recognizer_ready` — ТОТ ЖЕ объект, а не вторая такая же функция.
    Копия сказала бы «да» и «нет» в разных местах одного прогона."""
    assert Z.recognizer_ready is O.recognizer_ready
    assert Z.ocr_image is O.ocr_image
    assert Z.LOCAL_OCR_NOTE == O.LOCAL_OCR_NOTE


def test_кэш_спутник_работает_без_всякого_распознавателя(tmp_path, monkeypatch):
    """БЕСПЛАТНЫЙ СЛОЙ ПЕРВЫМ. Распознаём фото ОДИН раз: снятый текст лежит
    файлом-спутником `.ztxt` рядом с картинкой, и разбор идёт из него — без
    похода куда бы то ни было. Эта ветка одинаково работает на любой ОС и именно
    она держит разбор архива смен."""
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    photo = tmp_path / "shift_042.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")        # содержимое не важно: читается спутник
    (tmp_path / "shift_042.jpg.ztxt").write_text("Z-hisobot raqami 128\n",
                                                 encoding="utf-8")
    assert Z.transcribe(str(photo)) == ("Z-hisobot raqami 128", "cache")


def test_без_спутника_и_без_ocr_платный_вызов_не_подразумевается(tmp_path, monkeypatch):
    """`free_only=True` — платный внешний вызов не делается НИКОГДА, и функция
    честно говорит «нечем», а не молча уходит в сеть. Отдельный режим существует,
    чтобы это было ВИДНО в коде вызывающего."""
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    photo = tmp_path / "shift_043.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    assert Z.transcribe(str(photo)) == (None, "unavailable")
    assert Z.transcribe(str(photo), free_only=False) == (None, "needs_paid_api")


def test_текстовый_путь_не_зависит_от_фото(monkeypatch):
    """То же самое с другой стороны: без распознавателя разбор уже снятого текста
    работает полностью. Именно это и гоняется на Linux и Windows."""
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    r = pi.parse_invoice(G.load_corpus()["inv_01_header_pack"])
    assert [x["sum"] for x in r["lines"]] == [x["sum"] for x in DOC["lines"]]


# ── macOS: настоящий прогон Vision ─────────────────────────────────────────
@mac_only
@pytest.mark.parametrize("name,note", SHOTS, ids=IDS)
def test_фото_читается_локальным_ocr(name, note):
    """Фотография → дословный текст → разбор → сверка с бумагой.

    Допуск на огрехи распознавания честный и НЕСИММЕТРИЧНЫЙ: имя товара OCR
    имеет право прочитать неточно (`min_lines_ratio` строк обязаны совпасть по
    ЧИСЛАМ), а вот деньги — нет. Печатный итог обязан найтись, и неполный разбор
    обязан сам себя назвать неполным: молча проглотить потерянную строку нельзя."""
    text, err = O.ocr_image(os.path.join(PHOTOS, name))
    assert err is None, f"{name} ({note}): {err}"
    assert text and text.strip(), f"{name}: распознаватель вернул пустой текст"

    r = pi.parse_invoice(text)
    assert r is not None, f"{name}: разбор не признал это накладной\n{text}"

    grade = G.grade(DOC, r["lines"])
    assert grade["ok"] >= NEED_LINES, (f"{name} ({note}): строк «= бумага» {grade['ok']} из "
                                       f"{grade['total']}, долг {grade['debt']}\n{text}")
    assert r["total_sum"] == DOC["paper_total"], f"{name}: итог {r['total_sum']}\n{text}"
    assert r["lines_sum"] == DOC["paper_total"] or r["hard_mismatch"], (
        f"{name}: Σ строк {r['lines_sum']} ≠ итогу, а расхождение не названо жёстким")


@mac_only
def test_шапка_документа_читается_с_фотографии():
    """Номер и дата — не украшение: по ним заказ находит свой документ, а
    дедупликация не пускает одну накладную дважды. Знак «№» распознавание отдаёт
    как «Nº» — класс ошибки, а не особенность бланка."""
    text, err = O.ocr_image(os.path.join(PHOTOS, SHOTS[0][0]))
    assert err is None
    r = pi.parse_invoice(text)
    assert (r["number"], r["date"]) == (DOC["number"], DOC["date"]), text
    assert "ДЕМО-ТРЕЙД" in r["supplier"]


@mac_only
def test_реквизиты_из_шапки_деньгами_не_становятся():
    """Позиционное правило на живой фотографии: ИНН и расчётный счёт из шапки
    длинные, но деньгами быть не могут. Без этого правила «итогом документа»
    становился двадцатизначный номер счёта."""
    text, err = O.ocr_image(os.path.join(PHOTOS, SHOTS[0][0]))
    assert err is None
    bad = pi.doc_nonmoney(text)
    assert bad, "реквизиты шапки не опознаны — счёт и ИНН пойдут в кандидаты денег"
    assert all(v not in bad for v in pi.total_candidates(text))
    assert DOC["paper_total"] in pi.total_candidates(text)
