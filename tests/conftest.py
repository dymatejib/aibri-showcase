"""Общие фикстуры. Каждый прогон получает СВОЮ базу во временном каталоге —
тесты не видят ни живых данных, ни друг друга."""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Каталог создаём, ТОЛЬКО если его не задали снаружи: `setdefault` вычисляет
# аргумент всегда и плодил бы пустой временный каталог на каждом прогоне даже там,
# где переменная уже стоит.
if not os.environ.get("AIBRI_DATA_DIR"):
    os.environ["AIBRI_DATA_DIR"] = tempfile.mkdtemp(prefix="aibri_tests_")

_SEEDED_DIR = []


@pytest.fixture()
def fresh_db():
    """Чистая база на тест."""
    os.environ["AIBRI_DATA_DIR"] = tempfile.mkdtemp(prefix="aibri_case_")
    from aibri import db
    db.reset_db()
    return db


@pytest.fixture()
def seeded_db():
    """База со справочными паттернами — для тестов, которым нужны только правила
    данными (лейблы итога, единицы, маркеры терминалов).

    Фикстура ВЛАДЕЕТ своим каталогом и переставляет `AIBRI_DATA_DIR` на КАЖДОМ
    использовании. Иначе так: `fresh_db` уводит переменную на свою одноразовую
    базу, и тест, попросивший «засеянную», получал бы ту, что оказалась текущей, —
    сегодня случайно засеянную, завтра нет. Порядок запуска тестов не должен решать
    ничего. Каталог при этом один на сессию: сев паттернов идемпотентен."""
    if not _SEEDED_DIR:
        _SEEDED_DIR.append(tempfile.mkdtemp(prefix="aibri_seeded_"))
    os.environ["AIBRI_DATA_DIR"] = _SEEDED_DIR[0]
    from aibri import db
    db.init_db()
    return db
