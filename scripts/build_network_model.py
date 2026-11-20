"""Assemble and read back a pipe network; do NOT run a simulation (Py3.8+).

Uses actual PIPESIM 2021.1 interfaces from network_api.json. Makes a new copy
of a verified fluid-only model. Thermal configuration is explicitly pending.
"""

import argparse
import csv
import inspect
import json
import math
import shutil
import sys
import traceback
from pathlib import Path

from build_fluid_model import read_json, sha
from check_geometry import check_geometry


def clean(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, 'item'):
        return clean(value.item())
    return str(value)


def close_number(a, b, absolute=1e-8):
    try:
        return not isinstance(a, bool) and math.isfinite(float(a)) and math.isclose(float(a), float(b), rel_tol=1e-8, abs_tol=absolute)
    except (TypeError, ValueError):
        return False


def blank(value):
    return value is None or (not isinstance(value, (str, bool)) and math.isnan(float(value)))


def geometry_api_details(model_class):
    """Read the installed table adapter source, without constructing it."""
    result = {}
    try:
        namespace = getattr(model_class.set_geometry, '__globals__', {})
        cls = namespace.get('FlowlineGeometryProfile')
        if cls is not None:
            for label, obj in [('profile_class', cls),
                               ('set_data_frame', getattr(cls, 'set_data_frame', None)),
                               ('get_data_frame', getattr(cls, 'get_data_frame', None))]:
                if obj is not None:
                    try:
                        source = inspect.getsource(obj)
                        result[label] = {'text': source[:24000], 'truncated': len(source) > 24000}
                    except (OSError, TypeError):
                        result[label] = {'source_available': False}
    except Exception as error:
        result['inspection_error'] = str(error)
    return result


def units(model, context, parameter):
    d = model.describe(context=context, parameter=parameter)
    return {'units': d.units, 'symbol': d.units_symbol,
            'si_offset': getattr(d, 'si_offset', None), 'si_scale': getattr(d, 'si_scale', None)}


def unit_conversion(description, dimension):
    """Return API-to-SI gain and bias for explicitly recognized units."""
    symbol = (description['symbol'] or '').strip().lower().replace(' ', '').replace('³', '3')
    if dimension == 'temperature':
        table = {'k': (1.0, 0.0), 'degc': (1.0, 273.15), '°c': (1.0, 273.15),
                 'degf': (5 / 9, 273.15 - 32 * 5 / 9), '°f': (5 / 9, 273.15 - 32 * 5 / 9)}
    elif dimension == 'length':
        table = {k: (v, 0.0) for k, v in {'m': 1.0, 'mm': 0.001, 'cm': 0.01, 'ft': 0.3048, 'in': 0.0254}.items()}
    elif dimension == 'pressure':
        if ('gauge' in (description['units'] or '').lower()
                or 'barg' in symbol or abs(float(description['si_offset'] or 0)) > 1e-12):
            raise ValueError('Ожидалась абсолютная шкала давления: {}'.format(description))
        # The installed 2021.1 engine reports absolute SI pressure as "Pa a".
        # Whitespace normalization above turns this into "paa".
        table = {k: (v, 0.0) for k, v in {'pa': 1.0, 'paa': 1.0,
                                         'kpa': 1000.0, 'kpaa': 1000.0,
                                         'mpa': 1e6, 'mpaa': 1e6,
                                         'bar': 1e5, 'bara': 1e5,
                                         'psia': 6894.757293168}.items()}
    elif dimension == 'rate':
        table = {k: (v, 0.0) for k, v in {'sm3/s': 1.0, 'm3/s': 1.0,
                                         'sm3/d': 1 / 86400, 'm3/d': 1 / 86400,
                                         'sm3/day': 1 / 86400, 'm3/day': 1 / 86400}.items()}
    else:
        raise ValueError('Неизвестная размерность: ' + dimension)
    if symbol not in table:
        raise ValueError('Неподдерживаемые единицы {}: {}'.format(dimension, description))
    return table[symbol]


def value_si(model, context, parameter, dimension):
    description = units(model, context, parameter)
    gain, bias = unit_conversion(description, dimension)
    return float(model.get_value(context=context, parameter=parameter)) * gain + bias, description


def prepare(base, pipe_path, rates_path, settings_path):
    manifest = read_json(base.with_suffix('.json'))
    if manifest.get('status') != 'FLUID_MODEL_VERIFIED' or manifest.get('model_sha256') != sha(base):
        raise ValueError('Нужна неизменённая модель с успешным отчётом build_fluid_model')
    if manifest['input_hashes']['scenario_rates_json'] != sha(rates_path):
        raise ValueError('Используется другой scenario_rates.json')
    geometry = check_geometry(pipe_path)
    if not geometry['review_complete']:
        raise ValueError('В профиле есть точки с незавершённой сверкой')
    pipe, rates, settings = read_json(pipe_path), read_json(rates_path), read_json(settings_path)
    required = {'schema_version': 1, 'basis': 'initial_educational_network',
                'profile_coordinates': 'absolute_pipe_axis_elevation', 'profile_distance_input': 'horizontal'}
    for key, value in required.items():
        if settings.get(key) != value:
            raise ValueError('network_setup.json: неподдерживаемое значение ' + key)
    if settings['studies'] != {'base': 'base_stock_rates', 'plus10': 'plus10_stock_rates'}:
        raise ValueError('Ожидались сценарии base и plus10')
    if not close_number(settings['inlet_temperature_c'], 20) or not close_number(settings['outlet_pressure_bar_absolute'], 10):
        raise ValueError('Текущий учебный сценарий: 20 °C на входе, 10 bara на выходе')
    if settings['thermal']['configuration_status'] != 'pending':
        raise ValueError('Тепловой режим этим скриптом ещё не настраивается')
    profile = (pipe_path.parent / pipe['profile']).resolve()
    with profile.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    xyz = [{'x': float(r['horizontal_distance_m']), 's': float(r['axis_distance_m']),
            'z': float(r['pipe_axis_m'])} for r in rows]
    if len({pipe['from_node'], pipe['pipe_id'], pipe['to_node'], manifest['fluid_id']}) != 4:
        raise ValueError('Имена источника, трубы, выхода и флюида должны различаться')
    hashes = {'fluid_model': sha(base), 'fluid_manifest': sha(base.with_suffix('.json')),
              'pipe_json': sha(pipe_path), 'profile_csv': sha(profile), 'rates_json': sha(rates_path),
              'network_setup_json': sha(settings_path), 'builder_script': sha(Path(__file__)),
              'check_geometry_script': sha(Path(__file__).parent / 'check_geometry.py')}
    return manifest, pipe, rates, settings, geometry, xyz, hashes


def flow_correlations(settings, definitions):
    c, p = definitions.Constants, definitions.Parameters.FlowCorrelation
    chosen = settings['flow_correlations']
    if chosen['source'] != c.MultiphaseFlowCorrelationSource.BAKER_JARDINE:
        raise ValueError('Этот вариант использует корреляции Baker Jardine')
    if chosen['multiphase'] != c.MultiphaseFlowCorrelation.BakerJardine.BEGGSBRILLREVISED:
        raise ValueError('Этот вариант использует Beggs & Brill Revised')
    if chosen['single_phase'] != c.SinglePhaseFlowCorrelation.MOODY:
        raise ValueError('Этот вариант использует Moody для однофазного потока')
    if chosen['friction_multiplier'] != 1 or chosen['holdup_multiplier'] != 1:
        raise ValueError('Калибровочные множители в первом варианте равны 1')
    result = {p.SinglePhase.CORRELATION: chosen['single_phase'], p.SinglePhase.FACTOR: 1.0}
    for group in [p.Multiphase.Horizontal, p.Multiphase.Vertical]:
        result.update({group.SOURCE: chosen['source'], group.CORRELATION: chosen['multiphase'],
                       group.FRICTIONFACTOR: 1.0, group.HOLDUPFACTOR: 1.0})
    return result


def write_parameters(model, pipe, xyz, manifest, rates, settings, definitions, diagnostics=None):
    p, c = definitions.Parameters, definitions.Constants
    src, line, sink = pipe['from_node'], pipe['pipe_id'], pipe['to_node']
    scalar, raw, absent = [], [], []

    def operation(action, context, parameter):
        if diagnostics is not None:
            diagnostics['active_operation'] = {'action': action, 'context': context, 'parameter': parameter}

    def physical(context, parameter, dimension, si):
        operation('describe_units', context, parameter)
        d = units(model, context, parameter)
        gain, bias = unit_conversion(d, dimension)
        value = (float(si) - bias) / gain
        operation('set_value', context, parameter)
        model.set_value(context=context, parameter=parameter, value=value)
        scalar.append({'context': context, 'parameter': parameter, 'dimension': dimension,
                       'expected_si': float(si), 'assigned_value': value, 'units': d})

    def exact(context, parameter, value):
        operation('set_value', context, parameter)
        model.set_value(context=context, parameter=parameter, value=value)
        raw.append({'context': context, 'parameter': parameter, 'expected': value})

    def clear(context, parameter):
        operation('clear_value', context, parameter)
        model.set_value(context=context, parameter=parameter, value=definitions.NAN)
        absent.append({'context': context, 'parameter': parameter})

    exact(src, p.Source.ISACTIVE, True)
    exact(sink, p.Sink.ISACTIVE, True)
    exact(line, p.Flowline.ISACTIVE, True)
    exact(src, p.Source.ASSOCIATEDBLACKOILFLUID, manifest['fluid_id'])
    exact(src, p.Source.USEFLUIDOVERRIDES, False)
    exact(src, p.Source.USEPQCURVE, False)
    exact(src, p.Source.SELECTEDRATETYPE, c.FlowRateType.LIQUIDFLOWRATE)
    physical(src, p.Source.ELEVATION, 'length', xyz[0]['z'])
    physical(sink, p.Sink.ELEVATION, 'length', xyz[-1]['z'])
    physical(src, p.Source.TEMPERATURE, 'temperature', settings['inlet_temperature_c'] + 273.15)
    physical(src, p.Source.LIQUIDFLOWRATE, 'rate', float(rates['base_stock_rates']['liquid_m3_day']) / 86400)
    physical(sink, p.Sink.PRESSURE, 'pressure', settings['outlet_pressure_bar_absolute'] * 100000)
    for field in [p.Source.PRESSURE, p.Source.GASFLOWRATE, p.Source.MASSFLOWRATE]:
        clear(src, field)
    for field in [p.Sink.LIQUIDFLOWRATE, p.Sink.GASFLOWRATE, p.Sink.MASSFLOWRATE]:
        clear(sink, field)
    for field, value in [(p.Flowline.DETAILEDMODEL, True), (p.Flowline.USEGISDATA, False),
                         (p.Flowline.USEDEPTH, False), (p.Flowline.ENTERSURVEYLENGTH, False),
                         (p.Flowline.ENTEROD, False), (p.Flowline.ISFLIPPED, False)]:
        exact(line, field, value)
    for field, value in [(p.Flowline.INNERDIAMETER, pipe['inner_diameter_mm']),
                         (p.Flowline.WALLTHICKNESS, pipe['wall_thickness_mm']),
                         (p.Flowline.ROUGHNESS, pipe['roughness_mm'])]:
        physical(line, field, 'length', float(value) / 1000)
    # Only endpoint nodes have ItemView coordinates in the installed 2021.1
    # engine. Flowline exposes inherited X/Y constants but has no ItemView;
    # its diagram route follows its endpoint connections.
    # These coordinates are only a readable layout, not physical distances.
    for context, cls, x in [(src, p.Source, 0.0), (sink, p.Sink, 300.0)]:
        exact(context, cls.X_COORD, x)
        exact(context, cls.Y_COORD, 0.0)
    return scalar, raw, absent


def context_matches(value, name):
    if isinstance(value, dict):
        return any(context_matches(v, name) for v in value.values())
    text = str(value)
    return text == name or text.endswith(':' + name) or text.endswith('.' + name)


def boundary_row(tree, node):
    if isinstance(tree, dict):
        for key, value in tree.items():
            if context_matches(key, node) and isinstance(value, dict):
                return value
        for value in tree.values():
            result = boundary_row(value, node)
            if result is not None:
                return result
    return None


def boundary_si(row, key, gain, bias):
    try:
        return float(row[key]) * gain + bias
    except (KeyError, TypeError, ValueError):
        return float('nan')


def verify(model, data, scalar, raw, absent, correlation, definitions):
    manifest, pipe, rates, settings, geometry, xyz, _ = data
    p = definitions.Parameters
    result = {'scalar_parameters': [], 'exact_parameters': [], 'unassigned_boundaries': [], 'studies': {}}
    for row in scalar:
        actual, description = value_si(model, row['context'], row['parameter'], row['dimension'])
        tolerance = {'length': 1e-5, 'pressure': 0.1, 'temperature': 1e-6, 'rate': 1e-12}[row['dimension']]
        item = dict(row, actual_si=actual, actual_units=description, matches=close_number(actual, row['expected_si'], tolerance))
        result['scalar_parameters'].append(item)
    for row in raw:
        actual = model.get_value(context=row['context'], parameter=row['parameter'])
        expected = row['expected']
        matches = (actual is expected) if isinstance(expected, bool) else actual == expected
        result['exact_parameters'].append(dict(row, actual=actual, matches=bool(matches)))
    for row in absent:
        actual = model.get_value(context=row['context'], parameter=row['parameter'])
        result['unassigned_boundaries'].append(dict(row, actual=actual, matches=blank(actual)))
    frame = model.get_geometry(context=pipe['pipe_id'])
    result['geometry_columns'] = list(frame.columns)
    result['geometry_points'] = len(frame)
    if len(frame) != len(xyz):
        raise ValueError('Число точек профиля изменилось: {}'.format(len(frame)))
    required = {p.FlowlineGeometry.HORIZONTALDISTANCE: 'x', p.FlowlineGeometry.MEASUREDDISTANCE: 's',
                p.FlowlineGeometry.ELEVATION: 'z'}
    if not set(required) <= set(frame.columns):
        raise ValueError('Неожиданные столбцы get_geometry: {}'.format(list(frame.columns)))
    errors = {column: max(abs(float(frame.iloc[i][column]) - xyz[i][key]) for i in range(len(xyz)))
              for column, key in required.items()}
    result['geometry_max_errors_m'] = errors
    result['geometry_matches'] = all(v <= 1e-5 for v in errors.values())
    edges = model.connections()
    expected_edges = [(pipe['from_node'], pipe['pipe_id']), (pipe['pipe_id'], pipe['to_node'])]
    result['connections'] = edges
    result['connections_match'] = (len(edges) == 2 and all(
        any(context_matches(e.get('Source'), a) and context_matches(e.get('Destination'), b) for e in edges)
        for a, b in expected_edges))
    actual_corr = model.sim_settings.get_flow_correlations()
    result['flow_correlations'] = actual_corr
    # The public API documents the global settings under the Default key.
    global_corr = actual_corr.get('Default', {})
    result['flow_correlations_match'] = all(global_corr.get(k) == v for k, v in correlation.items())
    result['fluid_parameters_match'] = True
    for key, expected in manifest['assigned_parameters'].items():
        actual = model.get_value(context=manifest['fluid_id'], parameter=key)
        if key in ['DeadOilTemperature1', 'DeadOilTemperature2']:
            actual, _ = value_si(model, manifest['fluid_id'], key, 'temperature')
            expected += 273.15
        matches = actual == expected if isinstance(expected, (str, bool)) else close_number(actual, expected)
        if not matches:
            result['fluid_parameters_match'] = False
            result.setdefault('fluid_mismatches', {})[key] = {'expected': expected, 'actual': actual}
    for study, rate_key in settings['studies'].items():
        actual = model.tasks.networksimulation.get_conditions(use_surface_condition=True, study=study)
        src, sink = boundary_row(actual, pipe['from_node']), boundary_row(actual, pipe['to_node'])
        flow_condition = model.get_value(component=definitions.ModelComponents.NETWORKSIMULATION,
                                         Study=study, parameter=p.NetworkSimulation.FLOWRATECONDITION)
        phase_override = model.get_value(component=definitions.ModelComponents.NETWORKSIMULATION,
                                         Study=study, parameter=p.NetworkSimulation.USEPHASERATIO)
        # Boundary values share the engineering units of the corresponding nodes.
        source_rate_units = units(model, pipe['from_node'], p.Source.LIQUIDFLOWRATE)
        q_gain, q_bias = unit_conversion(source_rate_units, 'rate')
        t_gain, t_bias = unit_conversion(units(model, pipe['from_node'], p.Source.TEMPERATURE), 'temperature')
        pr_gain, pr_bias = unit_conversion(units(model, pipe['to_node'], p.Sink.PRESSURE), 'pressure')
        matches = (src is not None and sink is not None
                   and close_number(boundary_si(src, p.Boundary.LIQUIDFLOWRATE, q_gain, q_bias),
                                    float(rates[rate_key]['liquid_m3_day']) / 86400, 1e-12)
                   and close_number(boundary_si(src, p.Boundary.TEMPERATURE, t_gain, t_bias),
                                    settings['inlet_temperature_c'] + 273.15, 1e-6)
                   and blank(src.get(p.Boundary.PRESSURE))
                   and close_number(boundary_si(sink, p.Boundary.PRESSURE, pr_gain, pr_bias),
                                    settings['outlet_pressure_bar_absolute'] * 100000, 0.1)
                   and blank(sink.get(p.Boundary.LIQUIDFLOWRATE))
                   and flow_condition == definitions.Constants.FlowRateCondition.STOCKTANK
                   and phase_override is False)
        result['studies'][study] = {'conditions': actual, 'flow_rate_condition': flow_condition,
                                    'override_phase_ratios': phase_override, 'matches': bool(matches)}
    checks = ([v['matches'] for key in ['scalar_parameters', 'exact_parameters', 'unassigned_boundaries'] for v in result[key]]
              + [result[k] for k in ['geometry_matches', 'connections_match', 'flow_correlations_match', 'fluid_parameters_match']]
              + [v['matches'] for v in result['studies'].values()])
    result['status'] = 'PASS' if all(checks) else 'CHECK_REQUIRED'
    return result


def main():
    parser = argparse.ArgumentParser(description='Сборка участка АГЗУ-6 без запуска гидравлики')
    parser.add_argument('fluid_model', type=Path)
    parser.add_argument('pipe', type=Path)
    parser.add_argument('rates', type=Path)
    parser.add_argument('model', type=Path, help='НОВАЯ модель agzu6_network.pips')
    parser.add_argument('--settings', type=Path, default=Path(__file__).resolve().parents[1] / 'config' / 'network_setup.json')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    base, path = args.fluid_model.resolve(), args.model.resolve()
    output = path.with_suffix('.json')
    try:
        if path.suffix.lower() != '.pips' or path == base:
            raise ValueError('Укажите отдельный новый файл .pips')
        data = prepare(base, args.pipe.resolve(), args.rates.resolve(), args.settings.resolve())
        manifest, pipe, rates, settings, geometry, xyz, hashes = data
        if path.exists() or output.exists():
            old = read_json(output) if output.is_file() else {}
            if (path.is_file() and old.get('status') == 'NETWORK_STRUCTURE_VERIFIED'
                    and old.get('input_hashes') == hashes and old.get('model_sha256') == sha(path)):
                print('Network structure: ALREADY_BUILT');print('Отчёт: {}'.format(output));return 0
            raise ValueError('Выходной файл существует. Укажите другое имя, например agzu6_network_2.pips')
        print('Участок: {} -> {} -> {}'.format(pipe['from_node'], pipe['pipe_id'], pipe['to_node']))
        print('Профиль: {} точек; горизонталь {} м; ось {:.6f} м'.format(len(xyz), geometry['horizontal_length_m'], geometry['axis_chord_length_m']))
        if not args.apply:
            print('План проверен. Для сборки добавьте --apply.');return 0
        import pandas as pd
        import sixgill.definitions as d
        from sixgill.pipesim import Model
        corr = flow_correlations(settings, d)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError) as error:
        parser.error(str(error))
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'IN_PROGRESS', 'input_hashes': hashes, 'model_file': str(path),
              'configuration': settings, 'geometry_check': geometry, 'hydraulic_calculation_run': False,
              'calculation_ready': False, 'thermal_status': 'PENDING_ISOTHERMAL_CONFIGURATION',
              'correlation_physical_validation': 'NOT_ASSESSED',
              'geometry_comparison_tolerance_m': 1e-5,
              'tolerance_purpose': 'Import and readback precision, not field measurement accuracy'}
    report['geometry_api_details'] = geometry_api_details(Model)
    model, code = None, 0

    def stage(name):
        report['stage'] = name
        report.pop('active_operation', None)

    try:
        stage('copy_fluid_model')
        with path.open('xb') as target, base.open('rb') as source:
            shutil.copyfileobj(source, target)
        stage('open_network_copy')
        model = Model.open(str(path), units=d.Units.SI)
        stage('add_components')
        for component, name in [(d.ModelComponents.SOURCE, pipe['from_node']),
                                (d.ModelComponents.FLOWLINE, pipe['pipe_id']),
                                (d.ModelComponents.SINK, pipe['to_node'])]:
            if model.find(context=name):
                raise ValueError('Имя уже занято: ' + name)
            model.add(component=component, name=name)
        stage('connect_components')
        model.connect(source=pipe['from_node'], destination=pipe['pipe_id'])
        model.connect(source=pipe['pipe_id'], destination=pipe['to_node'])
        stage('set_component_parameters')
        scalar, raw, absent = write_parameters(model, pipe, xyz, manifest, rates, settings, d, diagnostics=report)
        stage('import_geometry')
        # Profile elevations are table entries, not the inherited scalar
        # Flowline.ELEVATION (that field belongs to SurfaceLocation objects).
        # In this SI session verify distance units on the line and elevation
        # units on its source node, where the scalar elevation actually exists.
        report['profile_import_unit_basis'] = []
        for context, parameter in [(pipe['pipe_id'], d.Parameters.Flowline.HORIZONTALDISTANCE),
                                    (pipe['from_node'], d.Parameters.Source.ELEVATION)]:
            report['active_operation'] = {'action': 'describe_profile_unit_basis',
                                           'context': context, 'parameter': parameter}
            descriptor = units(model, context, parameter)
            report['profile_import_unit_basis'].append({'context': context, 'parameter': parameter, **descriptor})
            if unit_conversion(descriptor, 'length') != (1.0, 0.0):
                raise ValueError('Для импорта профиля ожидались метры: {}'.format(descriptor))
        g = d.Parameters.FlowlineGeometry
        frame = pd.DataFrame({g.HORIZONTALDISTANCE: [r['x'] for r in xyz],
                              g.MEASUREDDISTANCE: [r['s'] for r in xyz],
                              g.ELEVATION: [r['z'] for r in xyz], g.ISVERTEX: [True] * len(xyz)})
        report['active_operation'] = {'action': 'set_geometry', 'context': pipe['pipe_id'],
                                       'rows': len(frame), 'columns': list(frame.columns)}
        model.set_geometry(context=pipe['pipe_id'], value=frame)
        stage('set_flow_correlations')
        model.sim_settings.use_global_flow_correlation = True
        model.sim_settings.set_flow_correlations({'Default': corr})
        p = d.Parameters
        stage('set_study_conditions')
        for study, rate_key in settings['studies'].items():
            report['active_operation'] = {'action': 'configure_study', 'study': study}
            model.add(component=d.ModelComponents.STUDY, name=study)
            q_gain, q_bias = unit_conversion(units(model, pipe['from_node'], p.Source.LIQUIDFLOWRATE), 'rate')
            t_gain, t_bias = unit_conversion(units(model, pipe['from_node'], p.Source.TEMPERATURE), 'temperature')
            pr_gain, pr_bias = unit_conversion(units(model, pipe['to_node'], p.Sink.PRESSURE), 'pressure')
            boundaries = {
                pipe['from_node']: {p.Boundary.PRESSURE: d.NAN,
                                    p.Boundary.TEMPERATURE: (settings['inlet_temperature_c'] + 273.15 - t_bias) / t_gain,
                                    p.Boundary.FLOWRATETYPE: d.Constants.FlowRateType.LIQUIDFLOWRATE,
                                    p.Boundary.LIQUIDFLOWRATE: (float(rates[rate_key]['liquid_m3_day']) / 86400 - q_bias) / q_gain},
                pipe['to_node']: {p.Boundary.PRESSURE: (settings['outlet_pressure_bar_absolute'] * 100000 - pr_bias) / pr_gain,
                                  p.Boundary.FLOWRATETYPE: d.Constants.FlowRateType.LIQUIDFLOWRATE,
                                  p.Boundary.LIQUIDFLOWRATE: d.NAN}}
            model.tasks.networksimulation.set_conditions(boundaries=boundaries, use_surface_condition=True, study=study)
            for parameter, value in [(p.NetworkSimulation.FLOWRATECONDITION, d.Constants.FlowRateCondition.STOCKTANK),
                                     (p.NetworkSimulation.USEPHASERATIO, False)]:
                model.set_value(component=d.ModelComponents.NETWORKSIMULATION, Study=study, parameter=parameter, value=value)
        stage('verify_before_save')
        report['before_save'] = verify(model, data, scalar, raw, absent, corr, d)
        if report['before_save']['status'] != 'PASS':
            raise ValueError('Проверка собранного участка до сохранения не пройдена')
        stage('save_and_close')
        model.save();model.close();model = None
        stage('reopen_and_verify')
        model = Model.open(str(path), units=d.Units.SI)
        report['after_reopen'] = verify(model, data, scalar, raw, absent, corr, d)
        if report['after_reopen']['status'] != 'PASS':
            raise ValueError('Проверка после повторного открытия не пройдена')
        stage('solver_validation')
        report['solver_validation'] = {}
        for study in settings['studies']:
            issues = model.tasks.networksimulation.validate(study=study)
            report['solver_validation'][study] = [
                {k: str(getattr(issue, k)) for k in ['message', 'path', 'property_name', 'severity'] if hasattr(issue, k)}
                for issue in issues]
        model.close();model = None
        report['model_sha256'] = sha(path)
        report['status'] = 'NETWORK_STRUCTURE_VERIFIED'
        stage('complete')
    except Exception as error:
        report['status'] = 'ERROR';report['error'] = '{}: {}'.format(type(error).__name__, error);code = 1
        report['traceback'] = traceback.format_exc()
    finally:
        if model is not None:
            try:
                model.close()
            except Exception as error:
                report['close_error'] = str(error);report['status'] = 'ERROR';code = 1
        report['source_fluid_model_unchanged'] = sha(base) == hashes['fluid_model']
        if not report['source_fluid_model_unchanged']:
            report['status'] = 'ERROR';report['error'] = 'Контрольная сумма исходной модели изменилась';code = 1
        with output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(clean(report), ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print('Network structure: {}'.format(report['status']))
    if 'error' in report:
        print(report['error'])
        print('Этап: {}'.format(report.get('stage', 'unknown')))
        if report.get('active_operation'):
            print('Операция: {}'.format(report['active_operation']))
    print('Отчёт: {}'.format(output))
    print('Гидравлика не запускалась. Далее нужна настройка изотермического режима.')
    return code


if __name__ == '__main__':
    sys.exit(main())
