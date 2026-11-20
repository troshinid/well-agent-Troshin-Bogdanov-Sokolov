"""Run base/plus10 in a NEW isothermal network copy (PIPESIM PTK 2021.1).

Requires the unchanged NETWORK_STRUCTURE_VERIFIED model and its JSON manifest.
Run and Restart are recorded separately. Checks below concern numerical
reproduction, not field accuracy or validity of the uncalibrated fluid model.
Python 3.8+. Uses the project's existing build_network_model.py helpers.
"""

import argparse
import csv
import json
import logging
import math
import re
import shutil
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path

from build_fluid_model import read_json, sha
from build_network_model import blank, boundary_row, close_number, unit_conversion, units, value_si


PSI_PA = 6894.757293168
BBL_M3 = 0.158987294928
# Allow for the rounded engineering-unit inputs written by PIPESIM 2021.1.
# These are numerical checks, NOT tolerances for field/model accuracy.
NUMERICAL_LIMITS = {
    'outlet_pressure_pa': 100.0,
    'temperature_k': 0.001,
    'rate_relative': 0.0001,
    'profile_length_m': 0.05,
    'profile_rise_m': 0.02,
    'run_restart_pressure_pa': 1.0,
}


def serial(value):
    """Strict JSON; retain missing numbers as null, never replace them with 0."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if hasattr(value, 'tolist'):
        return serial(value.tolist())
    if hasattr(value, 'item'):
        return serial(value.item())
    if hasattr(value, 'to_dict'):
        return serial(value.to_dict())
    return str(value)


def dump(path, value):
    path.write_text(json.dumps(serial(value), ensure_ascii=False, indent=2,
                               allow_nan=False) + '\n', encoding='utf-8')


def differences(expected, actual, path=''):
    """Report exact changed fields; never silently ignore a changed setting."""
    result = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child = path + '/' + str(key)
            if key not in expected or key not in actual:
                result.append({'path': child, 'expected': expected.get(key), 'actual': actual.get(key),
                               'missing_from': 'expected' if key not in expected else 'actual'})
            else:
                result.extend(differences(expected[key], actual[key], child))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            result.append({'path': path + '/length', 'expected': len(expected), 'actual': len(actual)})
        for index, (before, after) in enumerate(zip(expected, actual)):
            result.extend(differences(before, after, path + '/' + str(index)))
    elif expected != actual:
        result.append({'path': path, 'expected': expected, 'actual': actual})
    return result


class InputMismatch(ValueError):
    def __init__(self, check, expected, actual):
        self.details = {'check': check, 'differences': differences(expected, actual),
                        'expected': expected, 'actual': actual}
        first = self.details['differences'][:1]
        super().__init__('Изменены входные данные: {}; {}'.format(check, first))


def require_equal(expected, actual, check):
    if expected != actual:
        raise InputMismatch(check, expected, actual)


def canonical_connections(pairs):
    # Source/Destination/Source Port remain part of the comparison. Only the
    # order in which the API enumerates edges is immaterial to the topology.
    return sorted(serial(pairs), key=lambda edge: json.dumps(edge, sort_keys=True, ensure_ascii=False))


def finite(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('Ожидалось конечное число, получено {!r}'.format(value))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Отсутствующее или неконечное число')
    return number


def network_nodes(pairs):
    """Connections() does not promise traversal order."""
    if len(pairs) != 2:
        raise ValueError('Ожидалась одна линия Source -> Flowline -> Sink')
    candidates = [(a['Source'], a['Destination'], b['Destination'])
                  for a in pairs for b in pairs if a is not b and a['Destination'] == b['Source']]
    if len(candidates) != 1 or len(set(candidates[0])) != 3:
        raise ValueError('Не удалось однозначно определить Source -> Flowline -> Sink')
    return candidates[0]


def result_conversion(symbol, dimension):
    key = str(symbol).lower().replace(' ', '').replace('³', '3')
    if dimension == 'pressure_difference':
        if key == 'psi':
            return PSI_PA, 0.0
        dimension = 'pressure'
    if dimension == 'rate':
        extra = {'stb/d': BBL_M3 / 86400, 'stb/day': BBL_M3 / 86400,
                 'bbl/d': BBL_M3 / 86400, 'bbl/day': BBL_M3 / 86400,
                 'sbbl/day': BBL_M3 / 86400,
                 'mmscf/d': 28316.846592 / 86400,
                 'mmscf/day': 28316.846592 / 86400,
                 'scf/d': 0.028316846592 / 86400,
                 'scf/day': 0.028316846592 / 86400}
        if key in extra:
            return extra[key], 0.0
    return unit_conversion({'symbol': symbol, 'units': str(symbol),
                            'si_offset': 0.0}, dimension)


def pick_result(result):
    """Documented SimulationResult properties; retain schema errors in the report."""
    payload = {'result_type': '{}.{}'.format(type(result).__module__, type(result).__name__)}
    errors = {}
    for name in ['state', 'summary', 'messages', 'system', 'node', 'profile', 'profile_units']:
        try:
            payload[name] = serial(result[name] if isinstance(result, Mapping)
                                   else getattr(result, name))
        except Exception as error:
            errors[name] = '{}: {}'.format(type(error).__name__, error)
    if errors:
        payload['property_errors'] = errors
        payload['available_names'] = sorted(str(k) for k in (
            result.keys() if isinstance(result, Mapping) else dir(result)) if not str(k).startswith('_'))
    return payload


def variable_value(table, variable, node, dimension):
    row = table[variable]
    # Do not guess units or select another node when a field is unavailable.
    gain, bias = result_conversion(row['Unit'], dimension)
    return finite(row[node]) * gain + bias


def numeric_sequence(branch, name, profile_units, dimension):
    values = branch[name]
    if not isinstance(values, list) or not values:
        raise ValueError('Пустой или неизвестный формат профиля ' + name)
    gain, bias = result_conversion(profile_units[name], dimension)
    return [finite(value) * gain + bias for value in values]


def assess(payload, source, sink, expected_rates, temperature_k, outlet_pa,
           geometry, manual_psia=None):
    out = {'checks': {}, 'warnings': [], 'numerical_limits': NUMERICAL_LIMITS,
           'physical_validation': 'NOT_ASSESSED'}
    checks = out['checks']
    state = str(payload.get('state', '')).split('.')[-1].upper()
    checks['completed'] = state == 'COMPLETED'
    summary = payload.get('summary')
    checks['summary_available'] = isinstance(summary, dict) and 'Error' in summary and 'Warning' in summary
    checks['no_solver_errors'] = checks['summary_available'] and not summary['Error']
    checks['messages_available'] = isinstance(payload.get('messages'), list)
    if isinstance(summary, dict):
        out['warnings'].extend(summary.get('Warning') or [])
    # Console-only warnings must not disappear when Summary.Warning is empty.
    for message in payload.get('messages') or []:
        if re.search(r'\bwarning\b', str(message), re.I):
            if message not in out['warnings']:
                out['warnings'].append(message)
    out['property_errors'] = payload.get('property_errors', {})
    checks['result_properties_available'] = not out['property_errors']
    try:
        nodes = payload['node']
        pin = variable_value(nodes, 'Pressure', source, 'pressure')
        pout = variable_value(nodes, 'Pressure', sink, 'pressure')
        tin = variable_value(nodes, 'Temperature', source, 'temperature')
        tout = variable_value(nodes, 'Temperature', sink, 'temperature')
        out.update(inlet_pressure_pa_absolute=pin, outlet_pressure_pa_absolute=pout,
                   inlet_pressure_bar_absolute=pin / 1e5, pressure_difference_bar=(pin - pout) / 1e5,
                   inlet_temperature_c=tin - 273.15, outlet_temperature_c=tout - 273.15)
        checks['positive_absolute_pressures'] = pin > 0 and pout > 0
        checks['outlet_pressure'] = abs(pout - outlet_pa) <= NUMERICAL_LIMITS['outlet_pressure_pa']
        checks['node_temperatures'] = max(abs(tin - temperature_k), abs(tout - temperature_k)) <= NUMERICAL_LIMITS['temperature_k']
        out['stock_rates_m3_day'] = {}
        for phase in ['liquid', 'oil', 'water', 'gas']:
            variable = 'VolumeFlowrate{}StockTank'.format(phase.title())
            qin = variable_value(nodes, variable, source, 'rate') * 86400
            qout = variable_value(nodes, variable, sink, 'rate') * 86400
            target = finite(expected_rates[phase + '_m3_day'])
            out['stock_rates_m3_day'][phase] = {'source': qin, 'sink': qout, 'expected': target}
            checks[phase + '_rate'] = (math.isclose(qin, target, rel_tol=NUMERICAL_LIMITS['rate_relative'], abs_tol=1e-7)
                                       and math.isclose(qout, target, rel_tol=NUMERICAL_LIMITS['rate_relative'], abs_tol=1e-7))
            checks[phase + '_balance'] = math.isclose(qin, qout, rel_tol=NUMERICAL_LIMITS['rate_relative'], abs_tol=1e-7)
        if manual_psia is not None:
            reference = manual_psia * PSI_PA
            out['manual_comparison'] = {'manual_inlet_pressure_psia': manual_psia,
                                        'manual_inlet_pressure_pa_absolute': reference,
                                        'auto_minus_manual_pa': pin - reference,
                                        'assessment': 'DIFFERENCE_REPORTED_NOT_SCORED',
                                        'note': 'Rounded GUI reference; no physical-accuracy acceptance threshold assigned.'}
        branch, pu = payload['profile'][source], payload['profile_units']
        # Temperature is the GUI profile variable; also check TemperatureFluid
        # when supplied, so ambient temperature cannot stand in for fluid data.
        temps = numeric_sequence(branch, 'Temperature', pu, 'temperature')
        pressures = numeric_sequence(branch, 'Pressure', pu, 'pressure')
        distances = numeric_sequence(branch, 'TotalDistance', pu, 'length')
        elevations = numeric_sequence(branch, 'Elevation', pu, 'length')
        count = len(temps)
        checks['profile_columns_aligned'] = all(len(v) == count for v in [pressures, distances, elevations])
        checks['profile_point_count'] = count >= int(geometry['points'])
        checks['isothermal_profile'] = max(abs(t - temperature_k) for t in temps) <= NUMERICAL_LIMITS['temperature_k']
        if 'TemperatureFluid' in branch:
            tf = numeric_sequence(branch, 'TemperatureFluid', pu, 'temperature')
            checks['isothermal_fluid_profile'] = (len(tf) == count and
                max(abs(t - temperature_k) for t in tf) <= NUMERICAL_LIMITS['temperature_k'])
        checks['positive_profile_pressures'] = all(p > 0 for p in pressures)
        checks['profile_end_pressures'] = (abs(pressures[0] - pin) <= NUMERICAL_LIMITS['outlet_pressure_pa'] and
                                           abs(pressures[-1] - pout) <= NUMERICAL_LIMITS['outlet_pressure_pa'])
        checks['profile_distance_monotonic'] = all(b >= a for a, b in zip(distances, distances[1:]))
        length = distances[-1] - distances[0]
        rise = elevations[-1] - elevations[0]
        checks['profile_length'] = abs(length - geometry['axis_length_m']) <= NUMERICAL_LIMITS['profile_length_m']
        checks['profile_rise'] = abs(rise - geometry['rise_m']) <= NUMERICAL_LIMITS['profile_rise_m']
        out['profile'] = {'points': count, 'length_m': length, 'rise_m': rise,
                          'temperature_min_c': min(temps) - 273.15, 'temperature_max_c': max(temps) - 273.15}
        out['pressure_components_pa'] = {}
        for variable in ['CumulativeFrictionPressureDifference', 'CumulativeElevationPressureDifference',
                         'CumulativeAccelerationPressureDifference']:
            if variable in branch:
                values = numeric_sequence(branch, variable, pu, 'pressure_difference')
                out['pressure_components_pa'][variable] = values[-1] - values[0]
    except (KeyError, TypeError, ValueError, IndexError) as error:
        checks['required_results_readable'] = False
        out['extraction_error'] = '{}: {}'.format(type(error).__name__, error)
    out['status'] = ('CHECK_REQUIRED' if not all(checks.values()) else
                     'NUMERICAL_CHECKS_PASSED_WITH_WARNINGS' if out['warnings'] else 'NUMERICAL_CHECKS_PASSED')
    return out


def export_csv(folder, payload):
    for name in ['node', 'system']:
        table = payload.get(name)
        if not isinstance(table, dict):
            continue
        with (folder / (name + '.csv')).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['variable', 'unit', 'object', 'value'])
            for variable, row in table.items():
                if isinstance(row, dict):
                    for obj, value in row.items():
                        if obj != 'Unit':
                            writer.writerow([variable, row.get('Unit'), obj, value])
    for index, (branch, columns) in enumerate((payload.get('profile') or {}).items(), 1):
        if not isinstance(columns, dict):
            continue
        valid = {key: val for key, val in columns.items() if isinstance(val, list)}
        if not valid:
            continue
        # Index filenames avoid using model object names as file paths.
        with (folder / ('profile_{:02d}.csv'.format(index))).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['branch', 'row'] + list(valid))
            writer.writerow(['', 'unit'] + [(payload.get('profile_units') or {}).get(k, '') for k in valid])
            for i in range(max(len(v) for v in valid.values())):
                writer.writerow([branch, i] + [v[i] if i < len(v) else None for v in valid.values()])


def check_native_inputs(folder, files, expected_points, temperature_k):
    tnt = list(folder.rglob('*.tnt'))
    if len(tnt) != 1:
        raise ValueError('Ожидался один входной .tnt; файлы: {}'.format(files))
    text = tnt[0].read_bytes().decode('cp1251')
    # Strip engine comments before inspecting active commands.
    lines = [line.split('$', 1)[0].strip() for line in text.splitlines()]
    balances = re.findall(r'^HEAT\s+.*?\bBALANCE\s*=\s*(\w+)', '\n'.join(lines), re.I | re.M)
    declarations = re.findall(r'^UNITS\s+input\s*=\s*(\w+)', '\n'.join(lines), re.I | re.M)
    if not balances or any(b.upper() != 'OFF' for b in balances):
        raise ValueError('В .tnt не подтверждён HEAT BALANCE=OFF')
    if not declarations or len({v.upper() for v in declarations}) != 1:
        raise ValueError('Неизвестные или смешанные единицы входного .tnt')
    native_units = declarations[0].upper()
    if native_units not in ['ENG', 'METRIC', 'SI']:
        raise ValueError('Неизвестная система единиц .tnt: ' + native_units)
    temp_unit = {'ENG': 'degF', 'METRIC': 'degC', 'SI': 'K'}[native_units]
    gain, bias = result_conversion(temp_unit, 'temperature')
    temperatures = []
    for line in lines:
        if re.match(r'^node\s', line, re.I):
            match = re.search(r'\btemp\s*=\s*([-+\d.eE]+)', line, re.I)
            if match is None:
                raise ValueError('У точки NODE отсутствует температура')
            temperatures.append(finite(match.group(1)) * gain + bias)
    if len(temperatures) != expected_points or any(abs(t - temperature_k) > 0.001 for t in temperatures):
        raise ValueError('Число NODE или температуры в .tnt не совпадают с заданием')
    return {'file': str(tnt[0]), 'sha256': sha(tnt[0]), 'heat_balance': balances,
            'input_units': native_units, 'profile_nodes': len(temperatures),
            'temperature_min_k': min(temperatures), 'temperature_max_k': max(temperatures)}


def snapshot(model, manifest, source, line, sink, rates, definitions):
    """Read-only check of copied inputs against the verified build manifest."""
    p = definitions.Parameters
    baseline = manifest['after_reopen']
    result = {'scalars': [], 'exact': [], 'blank': [], 'conditions': {}}
    for row in baseline['scalar_parameters']:
        actual, _ = value_si(model, row['context'], row['parameter'], row['dimension'])
        if not close_number(actual, row['expected_si'], 1e-8):
            raise ValueError('Изменён параметр {}:{}'.format(row['context'], row['parameter']))
        result['scalars'].append(actual)
    for row in baseline['exact_parameters']:
        actual = model.get_value(context=row['context'], parameter=row['parameter'])
        if actual != row['expected']:
            raise ValueError('Изменён параметр {}:{}'.format(row['context'], row['parameter']))
        result['exact'].append(actual)
    for row in baseline['unassigned_boundaries']:
        actual = model.get_value(context=row['context'], parameter=row['parameter'])
        if not blank(actual):
            raise ValueError('Появилось лишнее граничное условие: ' + row['parameter'])
        result['blank'].append(None)
    result['geometry'] = serial(model.get_geometry(context=line).to_dict(orient='list'))
    result['connections'] = canonical_connections(model.connections())
    result['correlations'] = serial(model.sim_settings.get_flow_correlations())
    require_equal(canonical_connections(baseline['connections']), result['connections'], 'connections')
    require_equal(baseline['flow_correlations'], result['correlations'], 'flow_correlations')
    for study, rate_key in manifest['configuration']['studies'].items():
        actual = model.tasks.networksimulation.get_conditions(use_surface_condition=True, study=study)
        src, dst = boundary_row(actual, source), boundary_row(actual, sink)
        expected_src = boundary_row(baseline['studies'][study]['conditions'], source)
        expected_dst = boundary_row(baseline['studies'][study]['conditions'], sink)
        # Full boundary comparison also catches active flags, GOR and watercut.
        require_equal({'source': expected_src, 'sink': expected_dst},
                      {'source': serial(src), 'sink': serial(dst)}, 'boundaries:' + study)
        qgain, qbias = unit_conversion(units(model, source, p.Source.LIQUIDFLOWRATE), 'rate')
        q = finite(src[p.Boundary.LIQUIDFLOWRATE]) * qgain + qbias
        if not close_number(q, finite(rates[rate_key]['liquid_m3_day']) / 86400, 1e-12):
            raise ValueError('Расход сценария не совпадает с scenario_rates.json: ' + study)
        for field, expected in [(p.NetworkSimulation.FLOWRATECONDITION, definitions.Constants.FlowRateCondition.STOCKTANK),
                                (p.NetworkSimulation.USEPHASERATIO, False)]:
            if model.get_value(component=definitions.ModelComponents.NETWORKSIMULATION,
                               Study=study, parameter=field) != expected:
                raise ValueError('Изменены настройки фазовых расходов: ' + study)
        result['conditions'][study] = serial(actual)
    return result


def main():
    parser = argparse.ArgumentParser(description='Расчёт base/plus10 в новой изотермической копии PIPESIM')
    parser.add_argument('network_model', type=Path, help='Проверенная agzu6_network_4.pips и соседний .json')
    parser.add_argument('rates', type=Path, help='scenario_rates.json')
    parser.add_argument('output_dir', type=Path, help='Новая папка для модели и результатов')
    parser.add_argument('--manual-inlet-psia', type=float, nargs=2, metavar=('BASE', 'PLUS10'),
                        help='Входные давления из ручной проверки; разности записываются без оценки точности')
    parser.add_argument('--apply', action='store_true', help='Создать копию и запустить расчёты')
    args = parser.parse_args()
    source_model, rates_path, folder = args.network_model.resolve(), args.rates.resolve(), args.output_dir.resolve()
    manifest_path = source_model.with_suffix('.json')
    try:
        manifest, rates = read_json(manifest_path), read_json(rates_path)
        if source_model.suffix.lower() != '.pips' or manifest.get('status') != 'NETWORK_STRUCTURE_VERIFIED':
            raise ValueError('Нужна модель с отчётом NETWORK_STRUCTURE_VERIFIED')
        if sha(source_model) != manifest['model_sha256'] or sha(rates_path) != manifest['input_hashes']['rates_json']:
            raise ValueError('Контрольная сумма модели или расходов не совпала с отчётом сборки')
        if manifest['after_reopen']['status'] != 'PASS':
            raise ValueError('Не пройдена проверка исходной модели после открытия')
        config = manifest['configuration']
        if config['studies'] != {'base': 'base_stock_rates', 'plus10': 'plus10_stock_rates'}:
            raise ValueError('Ожидались сценарии base и plus10')
        if config['inlet_temperature_c'] != 20 or config['outlet_pressure_bar_absolute'] != 10:
            raise ValueError('Этот шаг рассчитан на 20 °C и выходное давление 10 bara')
        temperature_k, outlet_pa = 293.15, 1e6
        source, line, sink = network_nodes(manifest['after_reopen']['connections'])
        if folder.exists():
            raise ValueError('Выходная папка уже существует. Укажите новую папку для запуска')
        if args.manual_inlet_psia and any(finite(v) <= 0 for v in args.manual_inlet_psia):
            raise ValueError('Ручные давления должны быть положительными абсолютными')
        print('Сценарии: base, plus10; режим: 20 °C; выход: 10 bara')
        print('Новая папка: {}'.format(folder))
        if not args.apply:
            print('Входные файлы проверены. Для расчёта добавьте --apply.');return 0
        import sixgill.definitions as d
        from sixgill.pipesim import Model
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        parser.error(str(error))

    folder.mkdir(parents=True, exist_ok=False)
    copy_path, report_path = folder / 'isothermal_auto.pips', folder / 'report.json'
    report = {'status': 'IN_PROGRESS', 'stage': 'copy', 'source_model': str(source_model),
              'model_file': str(copy_path), 'source_sha256': sha(source_model),
              'manifest_sha256': sha(manifest_path), 'rates_sha256': sha(rates_path),
              'script_sha256': sha(Path(__file__)), 'numerical_limits': NUMERICAL_LIMITS,
              'physical_validation': 'NOT_ASSESSED', 'studies': {}}
    model, code = None, 0
    handler = logging.FileHandler(str(folder / 'sdk.log'), encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root, previous_level = logging.getLogger(), logging.getLogger().level
    root.addHandler(handler);root.setLevel(logging.INFO)
    try:
        with source_model.open('rb') as src, copy_path.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        report['stage'] = 'configure_thermal'
        model = Model.open(str(copy_path), units=d.Units.SI)
        original = snapshot(model, manifest, source, line, sink, rates, d)
        geom = original['geometry']
        g = d.Parameters.FlowlineGeometry
        geometry = {'points': len(geom[g.ELEVATION]),
                    'axis_length_m': finite(geom[g.MEASUREDDISTANCE][-1]) - finite(geom[g.MEASUREDDISTANCE][0]),
                    'rise_m': finite(geom[g.ELEVATION][-1]) - finite(geom[g.ELEVATION][0])}
        model.sim_settings.ambient_temperature = temperature_k
        model.sim_settings.network_keywords_bottom = 'HEAT BALANCE=OFF'
        # Preserve the known network configuration; do not insert other keywords.
        if str(model.sim_settings.single_branch_keywords or '').strip() or str(model.sim_settings.network_keywords_top or '').strip():
            raise ValueError('В исходной модели обнаружены дополнительные engine keywords')
        model.save();model.close();model = None
        report['stage'] = 'verify_thermal_after_reopen'
        model = Model.open(str(copy_path), units=d.Units.SI)
        report['ambient_temperature_api_k'] = finite(model.sim_settings.ambient_temperature)
        report['network_keywords_bottom'] = model.sim_settings.network_keywords_bottom
        if (abs(report['ambient_temperature_api_k'] - temperature_k) > 1e-6 or
                report['network_keywords_bottom'].strip().upper() != 'HEAT BALANCE=OFF'):
            raise ValueError('Тепловые настройки не сохранились')
        require_equal(original, snapshot(model, manifest, source, line, sink, rates, d), 'thermal_setup_readback')
        report['geometry'] = geometry
        task = model.tasks.networksimulation
        sv, pv = d.SystemVariables, d.ProfileVariables
        system_variables = [sv.PRESSURE, sv.TEMPERATURE, sv.VOLUME_FLOWRATE_LIQUID_STOCKTANK,
                            sv.VOLUME_FLOWRATE_OIL_STOCKTANK, sv.VOLUME_FLOWRATE_WATER_STOCKTANK,
                            sv.VOLUME_FLOWRATE_GAS_STOCKTANK, sv.FRICTION_DELTA_PRESSURE,
                            sv.ELEVATION_DELTA_PRESSURE]
        profile_variables = [pv.PRESSURE, pv.TEMPERATURE, pv.TEMPERATURE_FLUID, pv.TOTAL_DISTANCE,
                             pv.ELEVATION, pv.HOLDUP_FRACTION_LIQUID,
                             pv.CUMULATIVE_FRICTION_PRESSURE_DIFFERENCE,
                             pv.CUMULATIVE_ELEVATION_PRESSURE_DIFFERENCE,
                             pv.CUMULATIVE_ACCELERATION_PRESSURE_DIFFERENCE]
        for index, (study, rate_key) in enumerate(config['studies'].items()):
            study_folder = folder / study;study_folder.mkdir()
            item = report['studies'][study] = {'runs': {}}
            report['stage'] = study + ':validate'
            issues = task.validate(study=study)
            item['validation_issues'] = [
                {k: serial(getattr(issue, k)) for k in ['message', 'path', 'property_name', 'severity'] if hasattr(issue, k)}
                for issue in issues]
            if issues:
                raise ValueError('Перед расчётом обнаружены замечания валидации: ' + study)
            engine_folder = study_folder / 'engine_input';engine_folder.mkdir()
            report['stage'] = study + ':generate_engine_input'
            generated = task.generate_engine_files(folder_path=str(engine_folder), study=study)
            item['native_input'] = check_native_inputs(engine_folder, generated, geometry['points'], temperature_k)
            for restart in [False, True]:
                label = 'restart' if restart else 'run'
                report['stage'] = study + ':' + label
                run_folder = study_folder / label;run_folder.mkdir()
                print('{}: {} ...'.format(study, label), flush=True)
                # No boundary changes between each Run and its own Restart.
                # Each scenario starts with Restart=False to avoid reusing another scenario's BCs.
                result = task.run(study=study, use_surface_condition=True,
                                  profile_variables=profile_variables, system_variables=system_variables,
                                  options={'GenerateOutputFile': True, 'Restart': restart})
                payload = pick_result(result)
                dump(run_folder / 'results.json', payload)
                messages = payload.get('messages') or []
                (run_folder / 'engine_console.txt').write_text('\n'.join(str(m) for m in messages) + '\n', encoding='utf-8')
                export_csv(run_folder, payload)
                assessment = assess(payload, source, sink, rates[rate_key], temperature_k, outlet_pa,
                                    geometry, None if args.manual_inlet_psia is None else args.manual_inlet_psia[index])
                item['runs'][label] = assessment
                dump(report_path, report)
                print('{}: {} -> {}'.format(study, label, assessment['status']), flush=True)
                if assessment['status'] == 'CHECK_REQUIRED':
                    raise ValueError('Результат требует проверки: {} {}'.format(study, label))
            first, second = item['runs']['run'], item['runs']['restart']
            delta = second['inlet_pressure_pa_absolute'] - first['inlet_pressure_pa_absolute']
            item['repeatability'] = {'restart_minus_run_inlet_pressure_pa': delta,
                                     'matches': abs(delta) <= NUMERICAL_LIMITS['run_restart_pressure_pa']}
            if not item['repeatability']['matches']:
                raise ValueError('Входное давление изменилось при Restart: ' + study)
            # Readback catches cross-study condition changes and stale restart boundaries.
            report['stage'] = study + ':verify_after_runs'
            require_equal(original, snapshot(model, manifest, source, line, sink, rates, d), 'after_runs:' + study)
        report['stage'] = 'save_results'
        model.save();model.close();model = None
        report['model_sha256'] = sha(copy_path)
        base = report['studies']['base']['runs']['restart']['inlet_pressure_pa_absolute']
        plus = report['studies']['plus10']['runs']['restart']['inlet_pressure_pa_absolute']
        report['plus10_minus_base_inlet_pressure_pa'] = plus - base
        has_warnings = any(run['warnings'] for study in report['studies'].values() for run in study['runs'].values())
        report['status'] = 'NUMERICAL_CHECKS_PASSED_WITH_WARNINGS' if has_warnings else 'NUMERICAL_CHECKS_PASSED'
        report['stage'] = 'complete'
    except Exception as error:
        report['status'] = 'CHECK_REQUIRED';report['error'] = '{}: {}'.format(type(error).__name__, error)
        if isinstance(error, InputMismatch):
            report['input_mismatch'] = error.details
        report['traceback'] = traceback.format_exc();code = 1
    finally:
        if model is not None:
            try:
                model.close()
            except Exception as error:
                report['close_error'] = str(error);report['status'] = 'CHECK_REQUIRED';code = 1
        report['source_model_unchanged'] = sha(source_model) == report['source_sha256']
        if not report['source_model_unchanged']:
            report['status'] = 'CHECK_REQUIRED';report['source_hash_error'] = 'Исходная модель изменилась';code = 1
        dump(report_path, report)
        root.removeHandler(handler);handler.close();root.setLevel(previous_level)
    print('Hydraulics: {}'.format(report['status']))
    for study, item in report['studies'].items():
        final = item['runs'].get('restart') or item['runs'].get('run') or {}
        if 'inlet_pressure_bar_absolute' in final:
            print('{}: P входа = {:.6f} bara'.format(study, final['inlet_pressure_bar_absolute']))
    if report.get('error'):
        print(report['error']);print('Этап: ' + report['stage'])
    print('Отчёт: {}'.format(report_path))
    print('Это проверка численного воспроизведения. Промысловая точность не оценивалась.')
    return code


if __name__ == '__main__':
    sys.exit(main())
