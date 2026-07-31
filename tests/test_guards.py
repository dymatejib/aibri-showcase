"""Стражи гоняются и как отдельные скрипты, и отсюда.

Каждый страж возвращает объект с построчным отчётом; тест падает с ИМЕНАМИ
проваленных проверок, а не с абстрактным «assert False» — по выводу сразу видно,
какой закон нарушен.
"""
import pytest

from guards import (
    grep_guard_single_impl,
    mutation_control,
    oracle_stock_after_payment,
    scenario_guard,
)

GUARDS = [
    ("страж сценария", scenario_guard),
    ("грепстраж «один смысл = одна реализация»", grep_guard_single_impl),
    ("оракул «до оплаты склад не двигается»", oracle_stock_after_payment),
    ("мутационный контроль эталонов", mutation_control),
]


@pytest.mark.parametrize("title,module", GUARDS, ids=[g[1].__name__ for g in GUARDS])
def test_страж(title, module):
    g = module.main()
    assert not g.fails, f"{title}: провалы — {'; '.join(g.fails)}"
    assert g.checks, f"{title}: страж не сделал ни одной проверки"
