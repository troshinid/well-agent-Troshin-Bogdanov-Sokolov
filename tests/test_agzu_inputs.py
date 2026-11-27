"""Acceptance cases for the AGZU spreadsheet importer. Test data is synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from openpyxl import Workbook

from scripts.check_agzu import LAYOUT


ROOT = Path(__file__).resolve().parents[1]


class AgzuInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = Path(self.temp.name)
        self.source = data / 'raw' / 'agzu.xlsx'
        self.source.parent.mkdir()
        self.report = data / 'agzu_audit.json'

    def workbook(self, *, heading='Обводненность', water_cut=0.6):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Sheet1'
        for row in (2, 10, 20, 29):
            sheet[f'C{row}'] = 'Обводненность'
        for row, well, _ in LAYOUT:
            sheet[f'A{row}'] = well
            sheet[f'B{row}'] = 10
            sheet[f'C{row}'] = 0.5
            sheet[f'D{row}'] = 20
        sheet['C20'] = heading
        sheet['C21'] = water_cut
        sheet['C21'].number_format = '0%' if heading == 'Обводненность' else 'General'
        book.save(self.source)
        book.close()

    def run_import(self):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/check_agzu.py'), str(self.source)],
                              capture_output=True, text=True, encoding='utf-8',
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

    def check_refusal(self, phrase):
        result = self.run_import()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(phrase, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(self.report.exists())

    def test_percent_in_heading_requires_clarification(self):
        self.workbook(heading='Обводненность, %')
        self.check_refusal('C20')

    def test_fraction_above_one(self):
        self.workbook(water_cut=1.2)
        self.check_refusal('C21: обводнённость должна быть долей от 0 до 1')

    def test_unreadable_xlsx(self):
        self.source.write_bytes(b'not an xlsx workbook')
        self.check_refusal('Не удалось прочитать книгу agzu.xlsx')

    def test_xlsx_missing_content_types(self):
        self.workbook()
        damaged = self.source.with_name('damaged.xlsx')
        with ZipFile(self.source) as valid, ZipFile(damaged, 'w') as incomplete:
            for entry in valid.infolist():
                if entry.filename != '[Content_Types].xml':
                    incomplete.writestr(entry, valid.read(entry))
        damaged.replace(self.source)
        self.check_refusal('Не удалось прочитать книгу agzu.xlsx')

    def test_valid_stored_fraction_with_excel_percent_format(self):
        self.workbook()
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        audit = json.loads(self.report.read_text(encoding='utf-8'))
        well = next(row for row in audit['wells'] if row['well_id'] == '113')
        self.assertEqual(float(well['water_cut']), 0.6)
