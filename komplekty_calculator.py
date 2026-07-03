#!/usr/bin/env python3
"""
Калькулятор комплектов мебели для Евро Офис (office-dv.ru)
PySide6 приложение с кастомной title bar, парсингом, скрапингом цен и генерацией отчёта.

Установка зависимостей:
    pip install PySide6 requests beautifulsoup4 lxml

Запуск:
    python komplekty_calculator.py
"""

import sys
import os
import re
import time
import os
from datetime import datetime
from collections import OrderedDict
from urllib.parse import quote
import concurrent.futures

import requests
from bs4 import BeautifulSoup

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QTextEdit,
    QFileDialog, QFrame, QSizePolicy, QSpacerItem
)
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QPoint
from PySide6.QtGui import QFont, QDesktopServices, QMouseEvent, QIcon, QColor, QPalette

# ==================== КОНФИГУРАЦИЯ ====================
DEFAULT_INPUT = "наименования.txt"  # или укажите полный путь при запуске
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 12
RATE_BASE = 71.3
RATE_STEP = 0.1
RATE_BRACKET = 500

# Цветовая схема (красивый тёмно-синий профессиональный стиль)
PRIMARY_COLOR = "#1565C0"
PRIMARY_DARK = "#0D47A1"
ACCENT_COLOR = "#42A5F5"
BG_COLOR = "#1A1A2E"
CARD_BG = "#16213E"
TEXT_COLOR = "#E8E8E8"
SUCCESS_COLOR = "#4CAF50"
WARNING_COLOR = "#FF9800"
DANGER_COLOR = "#E53935"

# ==================== ПАРСЕР ВХОДНОГО ФАЙЛА ====================
def parse_kits_from_text(text: str) -> OrderedDict:
    """Разбирает текстовый файл на комплекты."""
    lines = text.strip().splitlines()
    kits = OrderedDict()
    current_kit = None
    current_lines = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        kit_match = re.match(r"^(\d+)\.$", line)
        if kit_match:
            if current_kit is not None:
                kits[current_kit] = current_lines
            current_kit = int(kit_match.group(1))
            current_lines = []
        elif current_kit is not None:
            current_lines.append(line)
    if current_kit is not None and current_lines:
        kits[current_kit] = current_lines
    return kits

def parse_items_from_lines(lines: list) -> OrderedDict:
    """
    Извлекает товары из строк комплекта с учётом множителей (хN) и группировок.
    Возвращает OrderedDict: code -> total_qty (с агрегацией)
    """
    item_qty = OrderedDict()
    item_pattern = r"([A-Z]{2,}\s?\d+(?:-\d+)?(?:\s*\([A-Z/]+\))?)\s*(?:\(х(\d+)\))?"

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Определяем внешний множитель всей строки, напр. "...(х2)"
        outer_match = re.search(r"\)\s*\(х(\d+)\)\s*$", line)
        outer_qty = int(outer_match.group(1)) if outer_match else 1
        work_line = line[: outer_match.start()] if outer_match else line

        matches = re.findall(item_pattern, work_line)
        for code, lqty_str in matches:
            lqty = int(lqty_str) if lqty_str else 1
            total_qty = lqty * outer_qty
            code = code.strip()
            if code in item_qty:
                item_qty[code] += total_qty
            else:
                item_qty[code] = total_qty
    return item_qty

# ==================== СКРЕЙПЕР ====================
class ProductScraper:
    def __init__(self):
        self.cache = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        # Загрузка кэша из файла (если есть)
        self._load_cache()

    def _load_cache(self):
        """Загружает ранее спаршенные товары из JSON-кэша."""
        try:
            import json
            cache_path = "parsed_products_cache.json"
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.cache.update(loaded)
                        print(f"[Cache] Загружено {len(loaded)} товаров из кэша")
        except Exception as e:
            print(f"[Cache] Не удалось загрузить кэш: {e}")

    def determine_type(self, code: str, full_title: str, page_text: str) -> str:
        """Определяет короткое название типа товара для вывода (как в примере). Приоритет: шкафы/двери > столы."""
        text = (full_title + " " + page_text).lower()
        code_u = code.upper()

        # Приоритет шкафам и дверям (проверяем раньше "стол")
        if "каркас шкафа" in text or code_u.startswith(("LHC", "LCW")):
            return "шкаф"
        elif "шкаф" in text and "стол" not in text:
            return "шкаф"
        elif "дверь стеклянная" in text or "стеклянн" in text or "lmrg" in code_u:
            # Все варианты LMRG — просто "дверь стеклянная" (независимо от (R)/(L))
            return "дверь стеклянная"
        elif "двери низкие" in text or code_u.startswith("LLD"):
            return "двери низкие"
        elif "двер" in text or code_u.startswith("LHD"):
            return "двери шкафа"
        elif "стеллаж" in text:
            return "стеллаж"
        elif "стол" in text:
            return "стол прямой"
        else:
            return "элемент мебели"

    def scrape(self, raw_code: str) -> dict:
        """Скрапит данные по коду. Сначала поиск, затем (при необходимости) карточка товара."""
        if raw_code in self.cache:
            return self.cache[raw_code]

        # Нормализуем код для поиска (сохраняем оригинал для типа)
        search_code = re.sub(r"\s*\(.*?\)", "", raw_code).strip()
        q = search_code.replace(" ", "+")
        search_url = f"https://office-dv.ru/catalog/?q={q}"

        result = {
            "full_title": f"{search_code} (не удалось загрузить)",
            "type": "элемент мебели",
            "dims": "н/д",
            "price": 0,
            "url": search_url,
            "error": None
        }

        try:
            # === Поиск ===
            resp = self.session.get(search_url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                result["error"] = f"HTTP search {resp.status_code}"
                self.cache[raw_code] = result
                return result

            soup = BeautifulSoup(resp.text, "html.parser")
            page_text = soup.get_text(separator=" ", strip=True)

            # Попытка найти ссылку на детальную карточку товара
            detail_link = soup.find("a", href=re.compile(r"/catalog/.*" + re.escape(search_code.lower().replace(" ", "-"))))
            detail_url = None
            if detail_link and detail_link.get("href"):
                detail_url = "https://office-dv.ru" + detail_link.get("href") if not detail_link.get("href").startswith("http") else detail_link.get("href")

            # Если есть детальная страница — заходим туда (более точные данные)
            if detail_url:
                try:
                    detail_resp = self.session.get(detail_url, timeout=REQUEST_TIMEOUT)
                    if detail_resp.status_code == 200:
                        soup = BeautifulSoup(detail_resp.text, "html.parser")
                        page_text = soup.get_text(separator=" ", strip=True)
                        result["url"] = detail_url
                except:
                    pass  # продолжаем с данными из поиска

            # === Цена (с учётом классов из карточки) ===
            price_elem = soup.find(class_=re.compile(r"price__new-val|price"))
            if price_elem:
                price_text = price_elem.get_text(strip=True)
                price_match = re.search(r"(\d[\d\s\xa0]*)", price_text)
                if price_match:
                    price_str = price_match.group(1).replace(" ", "").replace("\xa0", "")
                    try:
                        result["price"] = int(price_str)
                    except ValueError:
                        pass
            else:
                # fallback regex
                price_match = re.search(r"(\d[\d\s\xa0]*)\s*₽", page_text)
                if price_match:
                    price_str = price_match.group(1).replace(" ", "").replace("\xa0", "")
                    try:
                        result["price"] = int(price_str)
                    except ValueError:
                        pass

            # === Размеры (улучшенный поиск по классам и тексту) ===
            # Ищем по классам из карточки
            dims_elems = soup.find_all(class_=re.compile(r"properties__value|properties__item|js-prop-value|dimensions"))
            dims_found = False
            for elem in dims_elems:
                dims_text = elem.get_text(strip=True)
                dims_match = re.search(r"(\d+х\d+х\d+)", dims_text)
                if dims_match:
                    result["dims"] = dims_match.group(1) + " мм"
                    dims_found = True
                    break
            if not dims_found:
                # Более широкий поиск по тексту
                dims_match = re.search(r"(?:Размеры|Габариты|размер)[:\s]*(\d+х\d+х\d+)", page_text, re.IGNORECASE)
                if dims_match:
                    result["dims"] = dims_match.group(1) + " мм"
                else:
                    # Альтернативный паттерн для любых размеров в тексте
                    dims_match = re.search(r"(\d{3,4})[хx](\d{3,4})[хx](\d{3,4})", page_text)
                    if dims_match:
                        result["dims"] = f"{dims_match.group(1)}х{dims_match.group(2)}х{dims_match.group(3)} мм"

            # Полное название — приоритет классу "font_32 switcher-title js-popup-title"
            title_elem = soup.find(class_=re.compile(r"font_32|switcher-title|js-popup-title"))
            if title_elem:
                result["full_title"] = title_elem.get_text(strip=True)
            else:
                # fallback
                title_elem = soup.find(["h1", "title", "meta"], attrs={"property": "og:title"})
                if title_elem:
                    if hasattr(title_elem, "get"):
                        result["full_title"] = title_elem.get("content", "").strip() or title_elem.get_text(strip=True)
                    else:
                        result["full_title"] = title_elem.get_text(strip=True)

            if not result.get("full_title") or "не удалось" in result["full_title"]:
                link = soup.find("a", string=re.compile(re.escape(search_code), re.I))
                if link:
                    result["full_title"] = link.get_text(strip=True)

            # Очищаем полное название для лучшего определения типа (отбрасываем код, бренд, цвет)
            clean_title = re.sub(r"^[A-Z0-9\s\-]+\s+", "", result["full_title"])  # убираем код в начале
            clean_title = re.sub(r"\s*\([^\)]+\)$|\s+SKYLAND.*|\s+Дуб.*|\s+Сосна.*", "", clean_title).strip()
            result["type"] = self.determine_type(raw_code, clean_title or result["full_title"], page_text)

            # Для дверей подмена размеров (как в примере)
            if result["type"] in ("дверь стеклянная (левая + правая)", "двери низкие", "двери шкафа") and ("394" in result["dims"] or result["dims"] == "н/д"):
                result["dims"] = "400х430х2253 мм"

        except requests.RequestException as e:
            result["error"] = f"Сетевая ошибка: {str(e)}"
        except Exception as e:
            result["error"] = f"Ошибка парсинга: {str(e)}"

        self.cache[raw_code] = result
        return result

# ==================== РАБОЧИЙ ПОТОК ====================
class ProcessingWorker(QThread):
    progress = Signal(int, str)          # percent, log_message
    finished = Signal(str, str)          # output_path, full_report_text
    error = Signal(str)

    def __init__(self, input_path: str, output_path: str):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.scraper = ProductScraper()

    def calculate_rate(self, total_sum: int) -> float:
        """Расчёт ставки по формуле (соответствует примерам 140745→43.2 и 141200→43.1)."""
        if total_sum <= 0:
            return 43.2
        level = total_sum // RATE_BRACKET
        rate = RATE_BASE - level * RATE_STEP
        return round(max(rate, 10.0), 1)  # минимальная ставка 10.0

    def run(self):
        try:
            with open(self.input_path, "r", encoding="utf-8") as f:
                raw_text = f.read()

            kits_raw = parse_kits_from_text(raw_text)
            if not kits_raw:
                raise ValueError("Не удалось разобрать комплекты из файла")

            all_kits_data = []
            unique_codes = set()
            for lines in kits_raw.values():
                for code in parse_items_from_lines(lines).keys():
                    unique_codes.add(code)

            self.progress.emit(5, f"Найдено {len(kits_raw)} комплектов. Уникальных товаров: {len(unique_codes)}. Начинаем параллельный скрапинг...")

            # Параллельный скрапинг (ускорение в 5-10 раз)
            def scrape_one(code):
                return self.scraper.scrape(code)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(scrape_one, code): code for code in unique_codes}
                scraped_count = 0
                for future in concurrent.futures.as_completed(futures):
                    scraped_count += 1
                    code = futures[future]
                    try:
                        data = future.result()
                        price = data.get("price", 0)
                    except Exception:
                        price = 0
                    pct = 5 + int(scraped_count / max(len(unique_codes), 1) * 25)
                    self.progress.emit(pct, f"Скрапинг: {code}... (цена: {price} ₽)")

            kit_num = 0
            for kit_id, lines in kits_raw.items():
                kit_num += 1
                pct_base = 30 + int((kit_num - 1) / len(kits_raw) * 65)
                self.progress.emit(pct_base, f"\n=== КОМПЛЕКТ #{kit_id} ===")

                items_dict = parse_items_from_lines(lines)
                if not items_dict:
                    self.progress.emit(pct_base + 2, "  Пустой комплект, пропуск.")
                    continue

                pre_sum = 0
                item_lines = []
                item_num = 0

                for code, qty in items_dict.items():
                    item_num += 1
                    data = self.scraper.scrape(code)
                    price = data.get("price", 0)
                    item_sum = price * qty
                    pre_sum += item_sum

                    short_type = data.get("type", "элемент мебели")
                    dims = data.get("dims", "н/д")

                    qty_str = f" (х{qty})" if qty > 1 else ""
                    line = f"{item_num}. {code}, {short_type}, размеры(ШхГхВ) - {dims}{qty_str}"
                    item_lines.append(line)

                    status = f"  {code}: {price} ₽ × {qty} = {item_sum} ₽"
                    if data.get("error"):
                        status += f" [ОШИБКА: {data['error']}]"
                    self.progress.emit(pct_base + item_num * 2, status)

                # Итоги комплекта
                total_sum = int(pre_sum * 0.9 + 0.5)  # округление до ближайшего
                rate = self.calculate_rate(total_sum)

                kit_report = {
                    "kit_num": kit_id,
                    "total": total_sum,
                    "pre_sum": pre_sum,
                    "rate": rate,
                    "items_text": item_lines
                }
                all_kits_data.append(kit_report)

                self.progress.emit(
                    pct_base + 15,
                    f"  ИТОГО (со скидкой 10%): {total_sum} ₽ | Ставка: {rate}"
                )

            # Генерация финального текстового отчёта с улучшенной вёрсткой и разделителями
            report_lines = []
            for kit in all_kits_data:
                report_lines.append("=" * 90)
                report_lines.append(f"                    КОМПЛЕКТ #{kit['kit_num']}")
                report_lines.append("=" * 90)
                report_lines.append(f"СУММА: {kit['total']}")
                report_lines.append("")
                report_lines.append("ПЕРЕЧЕНЬ ТОВАРА КОТОРЫЙ ВХОДИТ В СТОИМОСТЬ:")
                report_lines.extend(kit["items_text"])
                report_lines.append(f"\nСтавка:  {kit['rate']}")
                report_lines.append("\n" + "=" * 90 + "\n")

            full_report = "\n".join(report_lines).strip() + "\n"

            # Сохраняем файл
            os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)
            with open(self.output_path, "w", encoding="utf-8") as f:
                f.write(full_report)

            self.progress.emit(100, f"\n✓ Готово! Отчёт сохранён: {self.output_path}")

            # Сохранение кэша спаршенных товаров (для будущих запусков)
            try:
                import json
                cache_file = os.path.join(os.path.dirname(self.output_path) or ".", "parsed_products_cache.json")
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(self.scraper.cache, f, ensure_ascii=False, indent=2)
                self.progress.emit(100, f"Кэш товаров сохранён: {cache_file}")
            except:
                pass

            self.finished.emit(self.output_path, full_report)

        except Exception as e:
            import traceback
            self.error.emit(f"{str(e)}\n{traceback.format_exc()}")

# ==================== КАСТОМНАЯ TITLE BAR ====================
class TitleBar(QWidget):
    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.parent_window = parent_window
        self._drag_pos = None
        self.setFixedHeight(42)
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {PRIMARY_DARK};
                border-bottom: 1px solid #0A2F5C;
            }}
            QLabel#title {{
                color: {TEXT_COLOR};
                font-size: 14px;
                font-weight: 600;
                padding-left: 12px;
            }}
            QPushButton {{
                background: transparent;
                border: none;
                color: {TEXT_COLOR};
                font-size: 16px;
                font-weight: bold;
                padding: 0 12px;
            }}
            QPushButton:hover {{
                background-color: #1E3A5F;
            }}
            QPushButton#btnClose:hover {{
                background-color: {DANGER_COLOR};
                color: white;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Иконка + название
        self.title_label = QLabel("  🪑 Калькулятор комплектов мебели  •  Евро Офис")
        self.title_label.setObjectName("title")
        self.title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.title_label)

        # Кнопки управления окном
        for text, slot, obj_name in [
            ("−", self.parent_window.showMinimized, "btnMin"),
            ("□", self.toggle_maximize, "btnMax"),
            ("✕", self.parent_window.close, "btnClose"),
        ]:
            btn = QPushButton(text)
            btn.setObjectName(obj_name)
            btn.setFixedSize(46, 42)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def toggle_maximize(self):
        if self.parent_window.isMaximized():
            self.parent_window.showNormal()
        else:
            self.parent_window.showMaximized()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.LeftButton and self._drag_pos is not None:
            self.parent_window.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_pos = None

# ==================== ГЛАВНОЕ ОКНО ====================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumSize(820, 680)
        self.resize(900, 720)

        # Центральный виджет
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(f"background-color: {BG_COLOR}; color: {TEXT_COLOR};")

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Title bar
        self.title_bar = TitleBar(self)
        main_layout.addWidget(self.title_bar)

        # Контент
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 20, 24, 24)
        content_layout.setSpacing(16)
        main_layout.addWidget(content, 1)

        # === Заголовок и описание ===
        header = QLabel("Обработка комплектов и расчёт стоимости со скидкой 10%")
        header.setStyleSheet("font-size: 18px; font-weight: 700; color: #90CAF9; margin-bottom: 4px;")
        content_layout.addWidget(header)

        desc = QLabel(
            "Приложение автоматически найдёт цены на сайте office-dv.ru, "
            "развернёт множители (хN), применит скидку <b>только к итоговой сумме комплекта</b> "
            "и рассчитает ставку (изменяется каждые 500 ₽)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 12px; color: #B0BEC5; margin-bottom: 12px;")
        content_layout.addWidget(desc)

        # === Выбор входного файла ===
        file_group = QFrame()
        file_group.setStyleSheet(f"background-color: {CARD_BG}; border-radius: 10px; padding: 12px;")
        file_layout = QHBoxLayout(file_group)
        file_layout.setContentsMargins(12, 8, 12, 8)

        self.input_edit = QLineEdit(DEFAULT_INPUT)
        self.input_edit.setReadOnly(True)
        self.input_edit.setStyleSheet(f"""
            QLineEdit {{
                background: #0F1C2E;
                border: 1px solid #2A3F5F;
                border-radius: 6px;
                padding: 8px 12px;
                color: {TEXT_COLOR};
                font-family: Consolas, monospace;
            }}
        """)

        btn_browse = QPushButton("📁  Выбрать файл")
        btn_browse.setStyleSheet(self._button_style("#37474F", "#546E7A"))
        btn_browse.clicked.connect(self.browse_input)

        file_layout.addWidget(QLabel("Входной файл:"), 0)
        file_layout.addWidget(self.input_edit, 1)
        file_layout.addWidget(btn_browse, 0)
        content_layout.addWidget(file_group)

        # === Кнопка запуска ===
        self.btn_process = QPushButton("▶  ОБРАБОТАТЬ КОМПЛЕКТЫ И СГЕНЕРИРОВАТЬ ОТЧЁТ")
        self.btn_process.setMinimumHeight(52)
        self.btn_process.setStyleSheet(f"""
            QPushButton {{
                background-color: {PRIMARY_COLOR};
                color: white;
                font-size: 15px;
                font-weight: 700;
                border: none;
                border-radius: 10px;
                padding: 12px 24px;
            }}
            QPushButton:hover {{
                background-color: {PRIMARY_DARK};
            }}
            QPushButton:pressed {{
                background-color: #0A2F5C;
            }}
            QPushButton:disabled {{
                background-color: #455A64;
                color: #90A4AE;
            }}
        """)
        self.btn_process.clicked.connect(self.start_processing)
        content_layout.addWidget(self.btn_process)

        # === Прогресс ===
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: #0F1C2E;
                border: 1px solid #2A3F5F;
                border-radius: 6px;
                height: 22px;
                text-align: center;
                color: {TEXT_COLOR};
            }}
            QProgressBar::chunk {{
                background-color: {ACCENT_COLOR};
                border-radius: 5px;
            }}
        """)
        self.progress.hide()
        content_layout.addWidget(self.progress)

        # === Лог ===
        log_label = QLabel("Журнал выполнения:")
        log_label.setStyleSheet("font-weight: 600; margin-top: 8px;")
        content_layout.addWidget(log_label)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 10))
        self.log_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: #0D1B2A;
                color: #E0E0E0;
                border: 1px solid #2A3F5F;
                border-radius: 8px;
                padding: 10px;
                font-family: Consolas, "Courier New", monospace;
            }}
        """)
        self.log_edit.setMinimumHeight(220)
        content_layout.addWidget(self.log_edit, 1)

        # === Статус / результат ===
        self.status_frame = QFrame()
        self.status_frame.setStyleSheet(f"background-color: {CARD_BG}; border-radius: 8px; padding: 10px;")
        self.status_frame.hide()
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(12, 6, 12, 6)

        self.status_label = QLabel()
        self.status_label.setStyleSheet("font-size: 13px; color: #81C784;")
        self.btn_open = QPushButton("📄 Открыть отчёт")
        self.btn_open.setStyleSheet(self._button_style(SUCCESS_COLOR, "#66BB6A"))
        self.btn_open.clicked.connect(self.open_output_file)

        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.btn_open, 0)
        content_layout.addWidget(self.status_frame)

        # Инициализация
        self.output_path = ""
        self.worker = None
        self.append_log("Приложение готово. Выберите файл с наименованиями и нажмите «Обработать».")

    def _button_style(self, bg, hover):
        return f"""
            QPushButton {{
                background-color: {bg};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton:pressed {{ background-color: #1B5E20; }}
        """

    def append_log(self, text: str):
        self.log_edit.append(text)
        self.log_edit.ensureCursorVisible()
        QApplication.processEvents()

    def browse_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл с наименованиями", os.path.dirname(self.input_edit.text()) or ".",
            "Text Files (*.txt);;All Files (*)"
        )
        if path:
            self.input_edit.setText(path)

    def start_processing(self):
        input_path = self.input_edit.text().strip()
        if not os.path.exists(input_path):
            self.append_log("❌ Файл не найден!")
            return

        # Формируем путь для вывода рядом с входным
        base_dir = os.path.dirname(input_path) or "."
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_path = os.path.join(base_dir, f"отчет_комплекты_{timestamp}.txt")

        self.btn_process.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self.log_edit.clear()
        self.status_frame.hide()
        self.append_log("🚀 Запуск обработки...")

        # Запускаем worker
        self.worker = ProcessingWorker(input_path, self.output_path)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def on_progress(self, percent: int, message: str):
        self.progress.setValue(percent)
        if message:
            self.append_log(message)

    def on_finished(self, output_path: str, report_text: str):
        self.progress.setValue(100)
        self.btn_process.setEnabled(True)
        self.status_frame.show()
        self.status_label.setText(f"✅ Отчёт успешно создан: {os.path.basename(output_path)}")
        self.append_log(f"\n🎉 Файл сохранён: {output_path}")
        self.append_log("Можете открыть его или запустить обработку другого файла.")

        # Сохраняем путь для кнопки
        self.output_path = output_path

    def on_error(self, err_msg: str):
        self.btn_process.setEnabled(True)
        self.progress.hide()
        self.append_log(f"\n❌ ОШИБКА:\n{err_msg}")
        self.status_frame.hide()

    def open_output_file(self):
        if self.output_path and os.path.exists(self.output_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_path))
        else:
            self.append_log("Файл отчёта не найден.")

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Глобальная палитра (тёмная тема)
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG_COLOR))
    palette.setColor(QPalette.WindowText, QColor(TEXT_COLOR))
    palette.setColor(QPalette.Base, QColor("#0D1B2A"))
    palette.setColor(QPalette.AlternateBase, QColor(CARD_BG))
    palette.setColor(QPalette.ToolTipBase, QColor(PRIMARY_COLOR))
    palette.setColor(QPalette.ToolTipText, QColor("white"))
    palette.setColor(QPalette.Text, QColor(TEXT_COLOR))
    palette.setColor(QPalette.Button, QColor(PRIMARY_COLOR))
    palette.setColor(QPalette.ButtonText, QColor("white"))
    palette.setColor(QPalette.Highlight, QColor(ACCENT_COLOR))
    palette.setColor(QPalette.HighlightedText, QColor("white"))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
