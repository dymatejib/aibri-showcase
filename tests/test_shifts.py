"""Кассовые смены: разбор Z-отчёта и закон «три расхождения не складываются»."""
import glob
import os

import pytest

from aibri.shifts import recon as R
from aibri.shifts import zreport as Z

ZDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "fixtures", "zreports")


def zt(name):
    with open(os.path.join(ZDIR, name + ".txt"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _db(seeded_db):
    """Аппараты и подписи полей приходят ДАННЫМИ, поэтому база нужна."""
    return seeded_db


# ── разбор Z ───────────────────────────────────────────────────────────────
def test_чистый_отчёт_читается_целиком():
    r = Z.parse_text(zt("z_01_clean"))
    assert (r["z_no"], r["cash"], r["card"], r["total"]) == (128, 1250000, 3400000, 4650000)
    assert r["confident"] is True and r["warnings"] == []


def test_число_разорванное_переносом_достраивается():
    """«350» на чеке — это 350 000: хвост съеден переносом строки. Достраиваем
    ТОЛЬКО потому, что прочитанный обрывок оказался ПРЕФИКСОМ числа, которое
    выводится из двух других проверенных сумм."""
    r = Z.parse_text(zt("z_02_wrapped"))
    assert (r["cash"], r["card"], r["total"]) == (350000, 900000, 1250000)
    assert r["repaired"] is True and r["confident"] is True


def test_обрывок_не_префикс_ничего_не_выдумываем():
    """Если достроенное число не подтверждается бумагой — парсер честно говорит
    «не сошлось» и оставляет прочитанное как есть. Смена уходит к человеку, а не
    в отчёт."""
    r = Z.parse_text(zt("z_04_unreadable"))
    assert r["cash"] == 412 and r["confident"] is False
    assert "нал+карта" in r["warnings"][0]


def test_широкий_кадр_с_битыми_подписями():
    """Самый частый реальный случай: Z и четыре слипа сняты одним фото, строки
    соседних бумаг сшиты в одну ленту, подписи полей побиты термопечатью
    («2-hisobot ragami», «Ochilishı sanasi», «Yopilish sallasi», «Umumiy sumına»),
    а сумма наличных разорвана чужой колонкой."""
    t = zt("z_03_wide_frame")
    assert Z.is_zreport(t) is True
    r = Z.parse_text(t)
    assert r["z_no"] == 137                       # класс «2»→«z»
    assert r["opened"] == "2026-07-03 08:05:00"   # класс вставленного знака
    assert r["closed"] == "2026-07-03 23:40:00"   # класс «ll»→«n»
    assert (r["cash"], r["card"], r["total"]) == (700000, 1800000, 2500000)
    assert r["confident"] is True


def test_свёртка_снимает_класс_ошибки_а_не_опечатку():
    from aibri.recognition import corpus
    assert corpus.ocr_fold("Yopilish sallasi")[0] == corpus.ocr_fold("Yopilish sanasi")[0]
    assert corpus.ocr_fold("2-hisobot ragami")[0] == corpus.ocr_fold("Z-hisobot raqami")[0]


def test_накладная_не_считается_z_отчётом():
    from aibri.recognition import golden as G
    assert Z.is_zreport(G.load_corpus()["inv_01_header_pack"]) is False


def test_маркер_не_z_в_обоих_написаниях():
    """Канонический маркер в коде был один, а записывалось второе написание.
    Порог «≥3 Z-слова» это спасал — и это ловушка: описательный текст слипа мог
    набрать три слова и быть разобран как фискальный отчёт."""
    for marker in ("NOT_Z_REPORT", "NOT_ZREPORT"):
        assert Z.is_zreport(zt("z_01_clean") + "\n" + marker) is False


def test_все_фикстуры_z_распознаются():
    for p in sorted(glob.glob(os.path.join(ZDIR, "*.txt"))):
        with open(p, encoding="utf-8") as f:
            assert Z.is_zreport(f.read()) is True, p


# ── L-21: три расхождения ──────────────────────────────────────────────────
SHIFT = {"revenue": 4_650_000, "cash": 1_250_000,
         "cash_open": 500_000, "cash_close": 1_712_500,
         "given": 0, "received": 0,
         "opened_ts": 0, "closed_ts": 50_000}
SLIPS = [("канал-A", 2_100_000), ("канал-B", 900_000), ("канал-C", 300_000),
         ("канал-D", 25_500)]


def test_столбик_денег_смены_сходится():
    m = R.shift_money(SHIFT, SLIPS)
    assert m["verified"] is True
    assert m["slips_sum"] == 3_325_500
    assert m["rest"] == 4_650_000 - 3_325_500 - 1_250_000    # 74 500
    assert m["state"] == "a"                                  # зона 50–100 тыс


def test_аппараты_приходят_данными():
    rows = R.shift_money(SHIFT, SLIPS)["app"]
    assert [r["name"] for r in rows] == ["Аппарат №1", "Аппарат №2"]
    assert rows[0]["amount"] == 2_100_000 + 900_000 + 300_000   # три канала одного
    assert rows[1]["amount"] == 25_500


def test_неизвестный_канал_не_приписывается_первому_аппарату():
    """«Чтобы сошлось» — не аргумент: канал без известного аппарата уходит
    отдельной строкой."""
    sl = R.split_slips([("канал-A", 1_000_000), ("неизвестный", 50_000)])
    assert sl["other"] == 50_000
    assert sl["rows"][0]["amount"] == 1_000_000


@pytest.mark.parametrize("value,want", [
    (0, "g"),
    (49_999, "g"),
    (50_000, "a"),        # «0-50 зелёная» кончилась ровно здесь
    (99_999, "a"),
    (100_000, "r"),       # владелец дословно: «100к+ красная»
    (100_001, "r"),
    (-100_000, "r"),      # знак расхождения зоны не меняет
])
def test_границы_зон_как_сказал_владелец(value, want):
    """Зоны названы вслух — значит, вслух названы и их КОНЦЫ. Обе границы
    закрываются с двух сторон: ровно 50 000 уже жёлтая, ровно 100 000 уже красная.
    Раньше сотня тысяч попадала в жёлтую — в единственной точке, которую владелец
    проговорил, светофор отвечал не то."""
    assert R.zone(value) == want


def test_мимо_ккм_нейтрально_и_без_зоны():
    z = {"z_no": 128, "card": 3_000_000, "cash": 1_250_000, "confident": True}
    k = R.shift_kkm(SHIFT, z, SLIPS)
    assert k["gap"] == 3_325_500 - 3_000_000
    assert k["label"] == "МИМО ККМ"
    assert k["state"] == "b"                     # синий, а не красный
    assert k["state"] not in ("g", "a", "r")     # зоной не красится вовсе


def test_отрицательный_разрыв_не_называется_мимо_ккм():
    """Слипов МЕНЬШЕ, чем карты в Z-отчёте, — это противоположный случай: не
    «прошло мимо кассы», а «мы собрали не все бумаги». Вердикт обязан брать подпись
    у самого счёта, а не писать её у себя второй раз."""
    z = {"z_no": 128, "card": 4_000_000, "cash": 1_250_000, "confident": True}
    k = R.shift_kkm(SHIFT, z, SLIPS)
    assert k["gap"] == 3_325_500 - 4_000_000        # отрицательный
    assert k["label"] == "НЕ СХОДИТСЯ · слипы не полные"
    v = R.shift_verdict(R.shift_money(SHIFT, SLIPS), k, R.shift_cashbox(SHIFT))
    chip = next(c for c in v["chips"] if c["amount"] == k["gap"])
    assert chip["label"] == k["label"]              # подпись одна на два места
    assert chip["kind"] == "b"                      # и по-прежнему нейтральная


def test_нет_бумаг_это_не_расхождение():
    """Смена без слипов не получает ни остатка, ни зоны, ни красного: вычитать
    нечего, и это вопрос сбора фотографий, а не кассира."""
    m = R.shift_money(SHIFT, [])
    assert m["rest"] is None and m["state"] == "mut"
    assert m["unverified"] == SHIFT["revenue"] - SHIFT["cash"]
    v = R.shift_verdict(m, R.shift_kkm(SHIFT, None, []), R.shift_cashbox(SHIFT))
    assert v["kind"] == "unchecked" and v["amount"] is None


def test_физкасса_считается_отдельно():
    box = R.shift_cashbox(SHIFT)
    assert box["has_fact"] is True
    assert box["expected"] == 500_000 + 1_250_000
    assert box["diff"] == 1_712_500 - 1_750_000       # −37 500
    assert box["state"] == "g" and "НЕДОСТАЧА" in box["label"]


def test_три_счёта_не_складываются_в_вердикте():
    z = {"z_no": 128, "card": 3_000_000, "cash": 1_250_000, "confident": True}
    rec = R.shift_recon(SHIFT, z, SLIPS)
    v = rec["verdict"]
    amounts = [c["amount"] for c in v["chips"] if c["amount"] is not None]
    assert sorted(amounts) == sorted([74_500, 325_500, -37_500])
    # ни одна попарная сумма трёх счетов не показывается как число вердикта
    assert v["amount"] == 74_500
    for a in (74_500 + 325_500, 74_500 - 37_500, 325_500 - 37_500):
        assert a not in amounts


def test_месяц_не_смешивает_излишки_и_недостачи():
    """На рабочем месяце излишки и недостачи были одного порядка, а нетто выходило
    на порядок меньше каждого из них — почерк размена, а не воровства. Увидеть его
    можно ТОЛЬКО не складывая."""
    z = {"z_no": 1, "card": 3_000_000, "cash": 1_250_000, "confident": True}
    plus = dict(SHIFT, cash_close=1_800_000)
    minus = dict(SHIFT, cash_close=1_600_000)
    tot = R.month_totals([R.shift_recon(plus, z, SLIPS), R.shift_recon(minus, z, SLIPS)])
    assert tot["box_plus"] == 50_000 and tot["box_minus"] == -150_000
    assert tot["kkm_gap"] == 2 * 325_500


def test_пустая_смена_не_портит_месяц():
    """Открыта и закрыта за минуты, выручки нет, а в поле пересчёта — опечатка.
    Одна такая строка дала бы месяцу фальшивую многомиллионную недостачу."""
    empty = {"revenue": 0, "cash": 0, "cash_open": 3_000_000, "cash_close": 300,
             "given": 0, "received": 0, "opened_ts": 0, "closed_ts": 40}
    assert R.is_empty_shift(empty) is True
    box = R.shift_cashbox(empty)
    assert box["empty_shift"] is True
    tot = R.month_totals([R.shift_recon(empty, None, [])])
    assert tot["box_minus"] == 0                    # в общий счёт не попала
    assert tot["empty_minus"] == 300 - 3_000_000    # но и не спрятана
