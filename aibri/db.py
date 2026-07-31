"""Минимальный слой SQLite — ЕДИНСТВЕННАЯ точка доступа к базе в витрине.

В боевом проекте это `app/core/db.py` (схема на ~40 таблиц, миграции колонок
через ALTER-и-проглотить-ошибку, WAL, busy_timeout). Здесь оставлено ровно то,
что нужно модулям витрины: `conn`/`one`/`q` и создание таблиц, которыми пользуются
леджер дилера и корпус документов.

ПОЧЕМУ ПИСАТЕЛЬСКИЙ КОНТРАКТ ОДИН: до дня, когда `BEGIN IMMEDIATE` встал в этот
слой, живой дефект выглядел так — правка цены и мгновенный тап по соседнему полю
шли двумя запросами на ОДНОМ соединении, SQLite апгрейдил транзакцию с чтения на
запись уже внутри неё и отдавал «database is locked» примерно в половине случаев.
Для владельца это читалось как «нажал — не сохранилось». Лечится не ретраями в
вызывающем коде, а тем, что писать умеет ровно один контекст-менеджер.
"""
import os
import sqlite3
import threading

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS dealers(
  id INTEGER PRIMARY KEY, name TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, name TEXT DEFAULT '', purchase_price REAL DEFAULT 0,
  stock REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, num TEXT DEFAULT '', dealer_id INTEGER,
  status TEXT DEFAULT 'в работе', total INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER,
  name TEXT DEFAULT '', qty REAL DEFAULT 0, cost REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock_moves(
  id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, kind TEXT DEFAULT '',
  qty REAL DEFAULT 0, price REAL DEFAULT 0, note TEXT DEFAULT '', ts TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS dealer_ledger(
  id INTEGER PRIMARY KEY, dealer_id INTEGER, ts TEXT DEFAULT '', kind TEXT DEFAULT '',
  amount INTEGER DEFAULT 0, order_id INTEGER, product_id INTEGER,
  qty REAL DEFAULT 0, qty_closed REAL DEFAULT 0,
  note TEXT DEFAULT '', author TEXT DEFAULT 'бот');
CREATE INDEX IF NOT EXISTS ix_ledger_lookup ON dealer_ledger(dealer_id, kind, order_id);
CREATE TABLE IF NOT EXISTS order_events(
  id INTEGER PRIMARY KEY, order_id INTEGER, author TEXT DEFAULT '',
  text TEXT DEFAULT '', ts TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS doc_corpus(
  id INTEGER PRIMARY KEY, file_path TEXT DEFAULT '', doc_kind TEXT DEFAULT '',
  parsed_json TEXT DEFAULT '', source TEXT DEFAULT 'patterns',
  confidence REAL DEFAULT 0, state TEXT DEFAULT 'live', golden INTEGER DEFAULT 0,
  withdrawn TEXT DEFAULT '', created TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_doc_corpus_file ON doc_corpus(file_path, id);
CREATE TABLE IF NOT EXISTS doc_patterns(
  id INTEGER PRIMARY KEY, doc_kind TEXT DEFAULT '', pattern_kind TEXT DEFAULT '',
  key TEXT DEFAULT '', value TEXT DEFAULT '', weight REAL DEFAULT 1,
  created TEXT DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS ix_doc_patterns_uniq
  ON doc_patterns(doc_kind, pattern_kind, key, value);
"""


def db_path():
    """Файл базы. `AIBRI_DATA_DIR` даёт каждому прогону тестов свой каталог."""
    d = os.environ.get("AIBRI_DATA_DIR") or os.path.join(os.getcwd(), "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "showcase.db")


def _raw():
    path = db_path()
    cur = getattr(_LOCAL, "path", None)
    if cur != path or getattr(_LOCAL, "c", None) is None:
        c = sqlite3.connect(path, timeout=10.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        _LOCAL.c, _LOCAL.path = c, path
    return _LOCAL.c


class _Writer:
    """Контекст записи. `BEGIN IMMEDIATE` берёт писательский замок СРАЗУ, а не при
    первом UPDATE внутри уже открытой читательской транзакции — иначе два
    параллельных «прочитал → записал» дают апгрейд транзакции и «database is
    locked» на ровном месте."""

    def __enter__(self):
        c = _raw()
        c.execute("BEGIN IMMEDIATE")
        self.c = c
        return c

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.c.commit()
        else:
            self.c.rollback()
        return False


def conn():
    return _Writer()


def q(sql, args=()):
    return [dict(r) for r in _raw().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = _raw().execute(sql, args).fetchone()
    return dict(r) if r else None


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
    from .recognition import corpus
    corpus.seed()


def reset_db():
    """Снести и пересоздать (используется фикстурами тестов)."""
    c = getattr(_LOCAL, "c", None)
    if c is not None:
        c.close()
    _LOCAL.c = None
    p = db_path()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(p + suffix)
        except OSError:
            pass
    init_db()
