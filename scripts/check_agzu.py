"""Проверка конкретной таблицы АГЗУ из исходного комплекта проекта."""

import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook


# Фиксируем строки скважин, чтобы не включить промежуточные итоги.
LAYOUT = [
    (3, "6000", "АГЗУ-7"),
    (4, "6014", "АГЗУ-7"),
    (5, "6004", "АГЗУ-7"),
    (6, "6", "АГЗУ-7"),
    (11, "60Р", "АГЗУ-60"),
    (12, "6001", "АГЗУ-60"),
    (21, "113", "АГЗУ-6"),
    (22, "114", "АГЗУ-6"),
    (23, "115", "АГЗУ-6"),
    (24, "116", "АГЗУ-6"),
    (30, "101", "АГЗУ-4"),
    (31, "102", "АГЗУ-4"),
    (32, "1сл", "АГЗУ-4"),
]
FIELDS = {"B": "liquid_rate", "C": "water_cut", "D": "gas_rate"}


def number(value, cell):
    """Пропуск сохраняем; ошибку, формулу или неверное число отклоняем."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{cell}: ожидалось число, получено {value!r}")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{cell}: ожидалось число, получено {value!r}") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{cell}: нужно конечное неотрицательное число")
    return result


def open_agzu_workbook(path, *, data_only):
    try:
        return load_workbook(path, read_only=True, data_only=data_only)
    except KeyError as error:
        # openpyxl looks up required XLSX parts by name inside the ZIP.
        raise ValueError(f"Не удалось прочитать книгу {path.name}: повреждён или неверен формат XLSX") from error


def audit(path):
    book = open_agzu_workbook(path, data_only=False)
    try:
        if "Sheet1" not in book.sheetnames:
            raise ValueError("Нет листа Sheet1: структура исходника изменилась")
        sheet = book["Sheet1"]
        # В исходном шаблоне C содержит долю. Заголовок с другой единицей
        # требует уточнения даже при числовом значении между 0 и 1.
        for row in (2, 10, 20, 29):
            cell = sheet[f"C{row}"]
            heading = str(cell.value or "").strip().casefold().replace("ё", "е")
            if heading not in {"обводненность", "обводненность, доля", "обводненность (доля)"}:
                raise ValueError(
                    f"Sheet1!C{row}: заголовок {cell.value!r}. "
                    "Уточните поле и единицу обводнённости: ожидается доля от 0 до 1."
                )
        wells = []
        for row, expected_id, group in LAYOUT:
            well_id = str(sheet[f"A{row}"].value).strip()
            if well_id != expected_id:
                raise ValueError(
                    f"A{row}: ожидалась скважина {expected_id}, получено {well_id}"
                )
            values = {
                field: number(sheet[f"{col}{row}"].value, f"{col}{row}")
                for col, field in FIELDS.items()
            }
            water_cut = values["water_cut"]
            if water_cut is not None and water_cut > 1:
                raise ValueError(f"C{row}: обводнённость должна быть долей от 0 до 1")
            missing = [field for field, value in values.items() if value is None]
            # F не используем: формула Excel может вернуть 0 при пустых входах.
            water_rate = (
                values["liquid_rate"] * water_cut
                if values["liquid_rate"] is not None and water_cut is not None
                else None
            )
            wells.append({
                "well_id": well_id,
                "group": group,
                "source_cells": {
                    field: f"Sheet1!{col}{row}" for col, field in FIELDS.items()
                },
                **values,
                "water_rate": water_rate,
                "missing_fields": missing,
                "status": "INCOMPLETE" if missing else "COMPLETE",
            })
        source_formula = sheet["C36"].value
    finally:
        book.close()

    cached = open_agzu_workbook(path, data_only=True)
    try:
        source_water_cut = number(cached["Sheet1"]["C36"].value, "C36")
    finally:
        cached.close()

    complete = [well for well in wells if well["status"] == "COMPLETE"]
    totals = {
        field: sum((well[field] for well in complete), Decimal(0))
        for field in ("liquid_rate", "water_rate", "gas_rate")
    }
    weighted = (
        totals["water_rate"] / totals["liquid_rate"]
        if totals["liquid_rate"] != 0 else None
    )
    normalized_formula = str(source_formula).upper().replace(" ", "")
    return {
        "source_file": path.name,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "units": {
            "liquid_rate": None,
            "gas_rate": None,
            "water_rate": None,
            "water_cut": "fraction",
        },
        "gas_standard_conditions": None,
        "calculation_ready": False,
        "note": "Суммы для проверки импорта в исходной шкале. Единицы и условия газа требуют подтверждения.",
        "wells": wells,
        "summary": {
            "rows": len(wells),
            "complete_rows": len(complete),
            "incomplete_rows": len(wells) - len(complete),
            "totals_scope": "Только строки с заполненными B, C и D",
            **totals,
            "weighted_water_cut": weighted,
            "weighted_water_cut_percent": weighted * 100 if weighted is not None else None,
        },
        "source_C36": {
            "formula": source_formula,
            "cached_value": source_water_cut,
            "cached_value_note": "Сохранённый результат Excel; openpyxl формулы не вычисляет",
            "known_missing_F32": normalized_formula == "=SUM(F3:F31)/B36",
            "recomputed_minus_cached": (
                weighted - source_water_cut
                if weighted is not None and source_water_cut is not None else None
            ),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Проверка исходной таблицы АГЗУ")
    parser.add_argument("xlsx", type=Path, help="Путь к исходному Excel в папке raw")
    args = parser.parse_args()
    source = args.xlsx.resolve()
    if not source.is_file() or source.suffix.lower() != ".xlsx":
        parser.error(f"Не найден файл .xlsx: {source}")
    try:
        report = audit(source)
    except BadZipFile:
        parser.error(f"Не удалось прочитать книгу {source.name}: повреждён или неверен формат XLSX")
    except (ValueError, OSError) as error:
        parser.error(str(error))

    # Отчёт сохраняется рядом с raw, за пределами исходной папки.
    output = source.parent.parent / "agzu_audit.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    summary = report["summary"]
    print(f"Строк скважин: {summary['rows']}")
    print(f"Полных: {summary['complete_rows']}; неполных: {summary['incomplete_rows']}")
    for well in report["wells"]:
        if well["missing_fields"]:
            print(f"Скважина {well['well_id']}: пропущены {', '.join(well['missing_fields'])}")
    print(f"Жидкость: {summary['liquid_rate']}")
    print(f"Вода: {summary['water_rate']}")
    print(f"Газ: {summary['gas_rate']}")
    percent = summary["weighted_water_cut_percent"]
    print(f"Обводнённость: {percent:.4f}%" if percent is not None else "Обводнённость не определена")
    if report["source_C36"]["known_missing_F32"]:
        print("Формула C36 пропускает F32")
    print(f"Отчёт: {output}")


if __name__ == "__main__":
    main()
