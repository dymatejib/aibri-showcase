"""Распознавание документов: корпус и паттерны (правила данными), геометрия
страницы и локальный OCR, парсер бумажных накладных, механизм эталонов с
мутационным контролем."""
from .paper_invoice import parse_invoice  # noqa: F401

__all__ = ["parse_invoice", "paper_invoice", "corpus", "ocr", "golden"]
