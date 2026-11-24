"""Find initialization APIs in the installed Toolkit without importing or running it."""

import argparse
import ast
import hashlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path


TERMS = re.compile(r'initial[\s_-]*(?:guess|estimate|pressure|condition)|'
                   r'network[\s_-]*estimate|pressure[\s_-]*estimate|\bestimates?\b|\bguesses?\b', re.I)
MAX_BYTES = 4 * 1024 * 1024
MAX_HITS = 180


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('p', 'div', 'li', 'tr', 'br', 'h1', 'h2', 'h3', 'pre'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1
        elif tag in ('p', 'div', 'li', 'tr', 'pre'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def scope(path):
    name = str(path).lower()
    return 'optimizer' if 'optim' in name else 'network' if 'network' in name or 'netsim' in name else 'other'


def scan(toolkit):
    toolkit = Path(toolkit).resolve()
    candidates = [toolkit / 'sixgill', toolkit / 'Lib/site-packages/sixgill']
    packages = [p for p in candidates if (p / 'pipesim.py').is_file() and (p / 'definitions.py').is_file()]
    if not packages:
        raise ValueError('Не найдены sixgill/pipesim.py и definitions.py внутри Toolkit.')
    roots = packages + [toolkit / name for name in ('Help', 'docs', 'Documentation', 'Examples')
                        if (toolkit / name).is_dir()]
    report = {'schema_version': 1, 'status': 'INSPECTION_COMPLETE',
              'toolkit_imported': False, 'model_opened': False, 'simulation_run': False,
              'initialization_changed': False, 'toolkit_root': str(toolkit),
              'files_scanned': 0, 'skipped': [], 'hits': [], 'public_model_api': [],
              'interpretation': 'Candidates only. Optimizer initialization is not a confirmed networksimulation API.'}
    paths = set()
    for root in roots:
        for path in root.rglob('*'):
            if path.suffix.lower() in {'.py', '.html', '.htm', '.rst', '.txt'} and path.is_file():
                if not path.is_symlink() and path.resolve().is_relative_to(toolkit):
                    paths.add(path.resolve())
    # Source files first, so Python method candidates are not displaced by docs.
    for path in sorted(paths, key=lambda p: (p.suffix.lower() != '.py', str(p))):
        relative = path.relative_to(toolkit).as_posix()
        if path.stat().st_size > MAX_BYTES:
            report['skipped'].append({'file': relative, 'reason': 'file_size_limit'})
            continue
        data = path.read_bytes()
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('cp1251', errors='replace')
        report['files_scanned'] += 1
        if path.suffix.lower() in {'.htm', '.html'}:
            html = PlainHTML()
            html.feed(text)
            text = ''.join(html.parts)
        lines = text.splitlines()
        if path.suffix.lower() == '.py':
            try:
                tree = ast.parse(text)
                for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and
                            node.name in {'Model', 'NetworkSimulation', 'AbstractNetworkSimulation', 'ModelSetting'}):
                    methods = []
                    for method in cls.body:
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and not method.name.startswith('_'):
                            methods.append({'name': method.name, 'line': method.lineno,
                                            'doc': (ast.get_docstring(method) or '')[:1400]})
                    report['public_model_api'].append({'file': relative, 'class': cls.name, 'methods': methods})
            except SyntaxError:
                report['skipped'].append({'file': relative, 'reason': 'ast_unavailable_text_still_searched'})
        selected = []
        previous_end = -1
        for index, line in enumerate(lines):
            if TERMS.search(line) and index > previous_end:
                first, last = max(0, index - 6), min(len(lines), index + 14)
                previous_end = last - 1
                selected.append({'line': index + 1, 'snippet': '\n'.join(lines[first:last])[:5000]})
                if len(selected) >= 10:
                    break
        if selected:
            if len(report['hits']) >= MAX_HITS:
                report['skipped'].append({'file': relative, 'reason': 'hit_file_limit'})
            else:
                report['hits'].append({'file': relative, 'scope_hint': scope(path),
                    'sha256': hashlib.sha256(data).hexdigest(), 'matches': selected})
    return report


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Поиск API начальных приближений без запуска PIPESIM')
    parser.add_argument('--registry', type=Path, required=True, help='Для чтения пути Toolkit из runner.json')
    parser.add_argument('--output', type=Path, required=True, help='Новый JSON вне реестра и Toolkit')
    args = parser.parse_args()
    output, registry = args.output.resolve(), args.registry.resolve()
    try:
        profile = json.loads((registry / 'runner.json').read_text(encoding='utf-8-sig'))
        toolkit = Path(profile['toolkit_root']).resolve()
        if output.suffix.lower() != '.json' or output.is_relative_to(registry) or output.is_relative_to(toolkit):
            raise ValueError('Укажи новый JSON вне реестра и Toolkit.')
        if output.exists():
            raise FileExistsError('Выходной файл уже существует. Укажи новое имя.')
        report = scan(toolkit)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('INSPECTION_ERROR: ' + str(error), file=sys.stderr)
        return 1
    print('Initialization API: INSPECTION_COMPLETE')
    print('Проверено файлов: {}; файлов с совпадениями: {}'.format(report['files_scanned'], len(report['hits'])))
    print('Модель не открывалась; расчёт и изменение настроек не выполнялись.')
    print('Отчёт: ' + str(output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
