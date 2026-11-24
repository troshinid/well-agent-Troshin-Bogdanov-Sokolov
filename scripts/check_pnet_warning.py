"""Inspect an existing registered run and collect text evidence. No PTK/API calls."""

import argparse
import hashlib
import json
import math
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.well_agent_mcp.results import Registry, ReportError, parse_json, read_bytes, utc_now
from src.well_agent_mcp.schemas import ReadRequest

COMPONENTS = ('CumulativeFrictionPressureDifference', 'CumulativeElevationPressureDifference',
              'CumulativeAccelerationPressureDifference')
WARNING = re.compile(r'Branch\s+([^:]+):\s*branch error is significant\s*\(([-+\d.eE]+)%\)', re.I)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def analyse(report):
    diagnostic = {'schema_version': 1, 'generated_at': utc_now(),
                  'assessment': 'CAUSE_NOT_ESTABLISHED', 'physical_validation': 'NOT_ASSESSED',
                  'note': 'A numerical match with a pressure ratio is an observation, not a definition of warning 26007903.',
                  'report_status': report.get('status'), 'stages': [], 'run_restart': {}}
    studies = report.get('studies', {})
    for scenario in ('base', 'plus10'):
        runs = studies.get(scenario, {}).get('runs', {})
        for stage in ('run', 'restart'):
            raw = runs.get(stage)
            row = {'scenario': scenario, 'stage': stage, 'available': isinstance(raw, dict)}
            diagnostic['stages'].append(row)
            if not isinstance(raw, dict):
                continue
            pin, pout = raw.get('inlet_pressure_pa_absolute'), raw.get('outlet_pressure_pa_absolute')
            valid = finite(pin) and finite(pout) and pin > 0 and pout > 0
            dp = pin - pout if valid else None
            ratio = 200 * abs(dp) / (pin + pout) if valid else None
            components = raw.get('pressure_components_pa', {})
            component_sum = (sum(components[k] for k in COMPONENTS)
                             if all(finite(components.get(k)) for k in COMPONENTS) else None)
            row.update(status=raw.get('status'), inlet_pressure_pa_absolute=pin,
                       outlet_pressure_pa_absolute=pout, pressure_drop_pa=dp,
                       relative_pressure_drop_percent=ratio,
                       relative_pressure_drop_formula='200 * abs(Pin - Pout) / (Pin + Pout)',
                       pressure_components_pa={key: components.get(key) for key in COMPONENTS},
                       component_sum_pa=component_sum,
                       drop_minus_components_pa=(dp - component_sum
                                                 if dp is not None and component_sum is not None else None),
                       failed_report_checks=[key for key, value in raw.get('checks', {}).items() if value is not True],
                       warnings=raw.get('warnings', []), warning_matches=[], stock_rates={})
            for message in row['warnings']:
                match = WARNING.search(str(message))
                if match:
                    percent = float(match.group(2))
                    if not math.isfinite(percent):
                        continue
                    row['warning_matches'].append({'branch': match.group(1).strip(),
                        'warning_percent': percent,
                        'warning_minus_pressure_ratio_percentage_points': percent - ratio if ratio is not None else None,
                        'matches_at_5_decimal_places': abs(percent - ratio) <= 0.000005 if ratio is not None else None})
            for phase in ('liquid', 'oil', 'water', 'gas'):
                values = raw.get('stock_rates_m3_day', {}).get(phase, {})
                source, sink, expected = (values.get(key) for key in ('source', 'sink', 'expected'))
                row['stock_rates'][phase] = {'source_sm3_day': source, 'sink_sm3_day': sink,
                    'expected_sm3_day': expected,
                    'sink_minus_source_percent': (100 * (sink - source) / abs(source)
                        if finite(source) and finite(sink) and source != 0 else None),
                    'source_minus_expected_percent': (100 * (source - expected) / abs(expected)
                        if finite(source) and finite(expected) and expected != 0 else None)}
        first, second = runs.get('run', {}), runs.get('restart', {})
        delta = {}
        for key in ('inlet_pressure_pa_absolute', 'outlet_pressure_pa_absolute'):
            a, b = first.get(key), second.get(key)
            delta[key + '_restart_minus_run_pa'] = b - a if finite(a) and finite(b) else None
        diagnostic['run_restart'][scenario] = delta
    diagnostic['all_stages_available'] = all(row['available'] for row in diagnostic['stages'])
    return diagnostic


def collect(registry, run_id, output):
    ReadRequest(run_id=run_id, scenario='compare')  # Validate before constructing any path.
    registry = Path(registry).resolve()
    output = Path(output).resolve()
    report, _ = Registry(registry).read(run_id)  # Verifies registered input/report hashes.
    entry = parse_json(read_bytes(registry / (run_id + '.json'), 'registry'), 'registry')
    report_path = Path(entry['files']['report']['path']).resolve()
    folder = report_path.parent
    if output.is_relative_to(folder) or output.is_relative_to(registry):
        raise ValueError('Сохрани диагностический архив вне реестра и папки расчёта.')
    diagnostic = analyse(report)
    diagnostic['run_id'] = run_id
    sources = [('report.json', report_path)]
    expected = []
    for scenario in ('base', 'plus10'):
        for stage in ('run', 'restart'):
            for name in ('results.json', 'engine_console.txt'):
                path = folder / scenario / stage / name
                expected.append(path)
                if path.is_file():
                    sources.append((path.relative_to(folder).as_posix(), path))
        native = folder / scenario / 'engine_input'
        if native.is_dir():
            for path in sorted(native.rglob('*.tnt')):
                if path.is_file():
                    sources.append((path.relative_to(folder).as_posix(), path))
    for name in ('sdk.log', 'isothermal_auto.out', 'isothermal_auto.log', 'isothermal_auto.err'):
        path = folder / name
        if path.is_file():
            sources.append((name, path))
    diagnostic['missing_stage_artifacts'] = [p.relative_to(folder).as_posix() for p in expected if not p.is_file()]
    diagnostic['native_tnt_files'] = [name for name, _ in sources if name.endswith('.tnt')]
    diagnostic['files'] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in sources:
            if path.is_symlink() or not path.resolve().is_relative_to(folder):
                raise ValueError('Диагностика не собирает ссылки на внешние файлы.')
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if name == 'report.json' and digest != entry['files']['report']['sha256']:
                raise ValueError('Отчёт изменился во время чтения.')
            archive.writestr(name, data)
            diagnostic['files'].append({'name': name, 'bytes': len(data), 'sha256': digest})
        archive.writestr('diagnostic.json', json.dumps(diagnostic, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    return diagnostic


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Диагностика предупреждения PNET по существующему расчёту')
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True, help='Новый ZIP вне реестра и папки расчёта')
    args = parser.parse_args()
    try:
        diagnostic = collect(args.registry, args.run_id, args.output)
    except (OSError, ValueError, KeyError, TypeError, ReportError) as error:
        print('DIAGNOSTIC_ERROR: ' + str(error), file=sys.stderr)
        return 1
    print('Диагностика сохранена; причина предупреждения пока не установлена.')
    for row in diagnostic['stages']:
        if row['available']:
            ratio = row['relative_pressure_drop_percent']
            text = 'нет данных' if ratio is None else '{:.8f}%'.format(ratio)
            print('{} / {}: относительный перепад {}; предупреждений {}'.format(
                row['scenario'], row['stage'], text, len(row['warnings'])))
    print('Отсутствующих файлов этапов: {}'.format(len(diagnostic['missing_stage_artifacts'])))
    print('Архив: ' + str(args.output.resolve()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
