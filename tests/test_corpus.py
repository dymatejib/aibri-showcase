"""Корпус документов и правила данными.

Отдельно проверяется закон «правку владельца стереть нельзя»: механизмы обучения
не воюют между собой — то, что один записал как правду владельца, другой не вправе
удалить.
"""
from aibri.recognition import corpus


def test_повтор_разбора_не_плодит_строк(fresh_db):
    payload = {"total": 300000, "lines": 3}
    assert corpus.record("photo_1.jpg", "invoice", payload, "patterns", 0.9) is True
    assert corpus.record("photo_1.jpg", "invoice", payload, "patterns", 0.9) is False
    assert len(corpus.corpus_for("photo_1.jpg")) == 1


def test_forget_снимает_но_не_удаляет(fresh_db):
    corpus.record("photo_1.jpg", "invoice", {"a": 1}, "patterns", 0.5)
    corpus.record("photo_1.jpg", "invoice", {"b": 2}, "owner_edit", 1.0)
    assert corpus.forget("photo_1.jpg", "invoice") is True
    assert corpus.corpus_for("photo_1.jpg") == []          # читатели не изменились
    assert len(corpus.owner_history("photo_1.jpg")) == 2   # история не короче


def test_правка_владельца_зачисляется_в_golden(fresh_db):
    """Раньше здесь стоял DELETE, и перевешивание одного алиаса физически
    уничтожало историю решений человека — из которой растёт корпус эталонов."""
    corpus.record("photo_2.jpg", "invoice", {"pick": "Лимонад Дюшес"}, "owner_edit", 1.0)
    corpus.forget("photo_2.jpg", "invoice")
    rows = corpus.golden_rows("invoice")
    assert len(rows) == 1 and rows[0]["golden"] == 1


def test_повторное_обучение_после_снятия_записывается(fresh_db):
    """Сравнение на дубль идёт с последней АКТУАЛЬНОЙ строкой: иначе повторное
    обучение тому же значению после снятия молча не записалось бы, и ключ остался
    бы пустым."""
    corpus.record("photo_3.jpg", "invoice", {"x": 1}, "owner_edit", 1.0)
    corpus.forget("photo_3.jpg", "invoice")
    assert corpus.record("photo_3.jpg", "invoice", {"x": 1}, "owner_edit", 1.0) is True
    assert len(corpus.corpus_for("photo_3.jpg")) == 1


def test_паттерн_учится_идемпотентно(fresh_db):
    assert corpus.learn_pattern("invoice", "unit_alias", "unit:box", "ящик") is True
    assert corpus.learn_pattern("invoice", "unit_alias", "unit:box", "ящик") is False
    vals = [r["value"] for r in corpus.patterns("invoice", "unit_alias")]
    assert "ящик" in vals


def test_секции_ищутся_с_учётом_регистра(fresh_db):
    """Заголовок секции печатается КАПСОМ. Без учёта регистра «JAMI» ловил бы
    слово «Jami» внутри строки поля из шапки и рвал разбор блоков."""
    text = "Jami cheklar soni: 214\nTO'LOVLAR\nUmumiy summa: 100\nJAMI\nUmumiy summa: 100"
    marks = corpus.sections(text, "zreport")
    names = [n for _p, n in marks]
    assert names == ["tolovlar", "jami"]


def test_свёртка_ищет_подпись_но_не_читает_значение(fresh_db):
    """Свёртка ЛОМАЕТ цифры (класс «2»→«z» — живой: номер Z-отчёта читался как
    «2-hisobot»), и это не мешает: по ней ищут ТОЛЬКО подпись поля, а значение
    берётся из ОРИГИНАЛЬНОГО текста по карте позиций."""
    text = "Umumiy summa: 2 500 000.00"
    folded, _starts, _ends = corpus.ocr_fold(text)
    assert "2500000" not in folded              # цифры свёрнуты, читать их нельзя
    hits = corpus.label_hits(text, "Umumiy summa")
    assert hits
    _s, end = hits[0]
    assert corpus.num_after(text, end) == 2500000   # значение — из оригинала
    assert corpus.label_hits("что угодно", "") == []   # пустой лейбл не матчит всё
