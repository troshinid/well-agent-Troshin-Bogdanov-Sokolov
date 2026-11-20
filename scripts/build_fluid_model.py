"""Reproduce the manual BlackOil setup in a NEW model (PIPESIM 2021.1, Py3.8+).

Without --apply, validate inputs and print the plan only. All model writes are
to a new path; a verified identical repeat returns ALREADY_BUILT. An optional
manual model is opened for readback only. No hydraulic task is run.
"""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

from check_fluid import check_fluid
from convert_reference_rates import convert
from create_fluid_draft import parameters_from_data


CORRELATIONS = {
    'solution_gas': ('SinglePointCalibration.SOLUTIONGAS', 'BlackOilCalibrationSolutionGas'),
    'oil_fvf': ('SinglePointCalibration.OILFVFCORRELATION', 'OilFVFCorrelation'),
    'live_oil_viscosity': ('LIVEOILVISCOSITYCORR', 'LiveOilViscosityCorrelation'),
    'undersaturated_oil_viscosity': ('UNDERSATURATEDOILVISCOSITYCORR', 'UndersaturatedOilViscosityCorrelation'),
    'gas_viscosity': ('SinglePointCalibration.GASVISCCORRELATION', 'GasViscCorrelation'),
    'gas_z': ('SinglePointCalibration.GASCOMPRESSCORRELATION', 'GasCompressCorrelation'),
}
CONTAMINANTS = {'CO2': 'FRACTIONCO2', 'H2S': 'FRACTIONH2S', 'N2': 'FRACTIONN2',
                'H2': 'FRACTIONH2', 'CO': 'FRACTIONCO'}
MANUAL_ABSOLUTE_TOLERANCES = {
    'DeadOilDensity': 0.0051, 'WaterCut': 0.0000051, 'GOR': 0.000001,
    'GasSpecificGravity': 0.00000051,
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def finite(value):
    if isinstance(value, bool):
        raise ValueError('Ожидалось число, получено bool')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Ожидалось конечное число')
    return result


def load_inputs(fluid, rates, settings):
    checked = check_fluid(fluid)
    r, s = read_json(rates), read_json(settings)
    original = r['input_rates']
    rebuilt = convert(checked, original['liquid_m3_day'], original['water_m3_day'],
                      original['gas_m3_day'], r['density_kg_m3']['oil_stock'])
    if json.loads(json.dumps(rebuilt, default=str)) != r:
        raise ValueError('scenario_rates.json не совпадает с пересчётом текущего fluid.json')
    required = {'schema_version': 1, 'target_toolkit': '2021.1', 'model': 'BlackOil',
                'units': 'PIPESIM_METRIC', 'basis': 'initial_uncalibrated_educational_case',
                'gas_ratio': 'GOR', 'water_ratio': 'WaterCut', 'dead_oil_viscosity': 'User2Point'}
    for key, expected in required.items():
        if s.get(key) != expected:
            raise ValueError('fluid_setup.json: неподдерживаемое значение {}'.format(key))
    if s['calibration']['mode'] != 'none':
        raise ValueError('Этот вариант создаёт флюид без PVT-калибровочных точек')
    if set(s['correlations']) != set(CORRELATIONS):
        raise ValueError('Неполный или неизвестный набор корреляций')
    if finite(s['water_specific_gravity']) != 1.0:
        raise ValueError('Текущий сценарий предполагает пресную воду с SG=1')
    fractions = s['gas_contaminant_correction_mole_fractions']
    if set(fractions) != set(CONTAMINANTS):
        raise ValueError('Нужны все пять долей газовых примесей')
    if any(finite(v) != 0 for v in fractions.values()):
        raise ValueError('Для текущего упрощённого сценария поправки примесей отключены')
    hashes = {'fluid_json': sha(fluid), 'scenario_rates_json': sha(rates),
              'fluid_setup_json': sha(settings), 'source_pdf': checked['source_sha256'],
              'builder_script': sha(Path(__file__))}
    for module in ['check_fluid.py', 'convert_reference_rates.py', 'create_fluid_draft.py']:
        hashes[module] = sha(Path(__file__).parent / module)
    return checked, r, s, hashes


def parameter(definitions, name):
    obj = definitions.Parameters.BlackOilFluid
    for part in name.split('.'):
        obj = getattr(obj, part)
    if not isinstance(obj, str):
        raise ValueError('Неизвестный параметр: ' + name)
    return obj


def enum(definitions, group, value):
    cls = getattr(definitions.Constants, group)
    if not any(getattr(cls, name) == value for name in dir(cls) if not name.startswith('_')):
        raise ValueError('{}: неизвестное значение {!r}'.format(group, value))
    return value


def build_plan(checked, rates, settings, definitions):
    plan = parameters_from_data(checked, definitions)

    def put(name, value):
        plan[parameter(definitions, name)] = value

    put('USEDEADOILDENSITY', True)
    put('DEADOILDENSITY', finite(rates['density_kg_m3']['oil_stock']))
    put('USEGASRATIO', enum(definitions, 'GasRatioOption', settings['gas_ratio']))
    put('GOR', finite(rates['manual_fields']['gor_sm3_per_sm3']))
    put('USEWATERRATIO', enum(definitions, 'WaterRatioOption', settings['water_ratio']))
    put('WATERCUT', finite(rates['manual_fields']['watercut_percent']))
    put('WATERSPECIFICGRAVITY', finite(settings['water_specific_gravity']))
    put('LIQUIDVISCOSITYCALC', enum(definitions, 'LiquidViscosityCalculationMethod', settings['mixture_viscosity']))
    for key, (name, group) in CORRELATIONS.items():
        put(name, enum(definitions, group, settings['correlations'][key]))
    # The inspected API exposes both names. Set and read both explicitly.
    put('SinglePointCalibration.LIVEOILVISCCORRELATION',
        enum(definitions, 'LiveOilViscCorrelation', settings['correlations']['live_oil_viscosity']))
    for which in ['above', 'below']:
        put('SinglePointCalibration.' + which.upper() + 'BBPTYPE',
            enum(definitions, 'OilCalibrationType', settings['calibration'][which + '_bp_property']))
    for name, field in CONTAMINANTS.items():
        put(field, finite(settings['gas_contaminant_correction_mole_fractions'][name]))
    return plan


def check_units(model, context, definitions):
    expected = {'DEADOILTEMPERATURE1': 'degC', 'DEADOILTEMPERATURE2': 'degC',
                'DEADOILVISCOSITY1': 'kg/(m.s)', 'DEADOILVISCOSITY2': 'kg/(m.s)',
                'DEADOILDENSITY': 'kg/m3', 'WATERCUT': '%', 'GOR': 'sm3/sm3',
                'GASSPECIFICGRAVITY': '', 'WATERSPECIFICGRAVITY': ''}
    result = {}
    for name, symbol in expected.items():
        key = parameter(definitions, name)
        description = model.describe(context=context, parameter=key)
        if (description.units_symbol or '').strip() != symbol:
            raise ValueError('{}: неожиданные единицы {!r}'.format(key, description.units_symbol))
        if not symbol and description.units != 'dimensionless':
            raise ValueError(key + ': ожидается безразмерная величина')
        result[key] = {'units': description.units, 'units_symbol': description.units_symbol}
    return result


def compare(model, context, plan, manual=False):
    result = {}
    for key, expected in plan.items():
        actual = model.get_value(context=context, parameter=key)
        if isinstance(expected, bool):
            matches = isinstance(actual, bool) and actual is expected
        elif isinstance(expected, str):
            matches = actual == expected
        else:
            try:
                actual = finite(actual)
                tolerance = MANUAL_ABSOLUTE_TOLERANCES.get(key, 1e-7) if manual else 1e-10
                matches = math.isclose(actual, expected, rel_tol=1e-8, abs_tol=tolerance)
            except (ValueError, TypeError):
                matches = False
        if isinstance(actual, float) and not math.isfinite(actual):
            actual = None
        result[key] = {'expected': expected, 'actual': actual, 'matches': bool(matches)}
    return {'status': 'PASS' if all(v['matches'] for v in result.values()) else 'CHECK_REQUIRED',
            'parameters': result}


def calibration_snapshot(model, context, definitions):
    cls = definitions.Parameters.BlackOilFluid.SinglePointCalibration
    result = {}
    for name in dir(cls):
        if not name.endswith(('_VALUE', '_PRESSURE', '_TEMPERATURE')):
            continue
        key = getattr(cls, name)
        value = model.get_value(context=context, parameter=key)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            result[key] = None
        else:
            raise ValueError('Неожиданно заполнено калибровочное поле {}={!r}'.format(key, value))
    return result


def inspect_model(model, context, plan, definitions, manual=False):
    return {'units': check_units(model, context, definitions),
            'comparison': compare(model, context, plan, manual),
            'pvt_calibration_points': calibration_snapshot(model, context, definitions)}


def main():
    parser = argparse.ArgumentParser(description='Воспроизведение настройки флюида PIPESIM')
    parser.add_argument('fluid', type=Path)
    parser.add_argument('rates', type=Path)
    parser.add_argument('model', type=Path, help='Новая автоматическая модель .pips')
    parser.add_argument('--settings', type=Path, default=Path(__file__).resolve().parents[1] / 'config' / 'fluid_setup.json')
    parser.add_argument('--manual-model', type=Path, help='Сохранённая вручную модель для сравнения')
    parser.add_argument('--apply', action='store_true', help='Создать новую модель после проверки входа')
    args = parser.parse_args()
    path = args.model.resolve()
    output = path.with_suffix('.json')
    manual = args.manual_model.resolve() if args.manual_model else None
    try:
        if path.suffix.lower() != '.pips' or (manual and (manual == path or not manual.is_file())):
            raise ValueError('Нужен новый путь .pips и отдельная существующая ручная модель')
        checked, rates, settings, hashes = load_inputs(args.fluid.resolve(), args.rates.resolve(), args.settings.resolve())
        if manual:
            hashes['manual_model'] = sha(manual)
        if path.exists() or output.exists():
            previous = read_json(output) if output.is_file() else {}
            if (path.is_file() and previous.get('status') == 'FLUID_MODEL_VERIFIED'
                    and previous.get('input_hashes') == hashes and previous.get('model_sha256') == sha(path)):
                print('Fluid model: ALREADY_BUILT')
                print('Отчёт: {}'.format(output))
                return 0
            raise ValueError('Выходной файл уже существует. Укажите новое имя, например t1_fluid_auto_2.pips')
        print('Новая модель: {}'.format(path))
        print('Расход жидкости, база: {} sm3/day'.format(rates['base_stock_rates']['liquid_m3_day']))
        print('GOR: {} sm3/sm3; Watercut: {} %'.format(rates['manual_fields']['gor_sm3_per_sm3'], rates['manual_fields']['watercut_percent']))
        print('Корреляции: {}'.format(json.dumps(settings['correlations'], ensure_ascii=False)))
        if not args.apply:
            print('План проверен. Для создания модели добавьте --apply.')
            return 0
        import sixgill.definitions as definitions
        from sixgill.pipesim import Model
        if definitions.Units.METRIC != settings['units']:
            raise ValueError('В установленном API отличается система единиц')
        plan = build_plan(checked, rates, settings, definitions)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError) as error:
        parser.error(str(error))

    path.parent.mkdir(parents=True, exist_ok=True)
    context = checked['fluid_id']
    report = {'status': 'IN_PROGRESS', 'fluid_id': context, 'model_file': str(path),
              'input_hashes': hashes, 'configuration': settings, 'assigned_parameters': plan,
              'hydraulic_calculation_run': False, 'pvt_lab_validation': 'NOT_ASSESSED',
              'base_stock_rates': rates['base_stock_rates'], 'plus10_stock_rates': rates['plus10_stock_rates'],
              'comparison_tolerances': {'automated_relative': 1e-8, 'automated_absolute': 1e-10,
                                       'manual_default_absolute': 1e-7, 'manual_absolute_overrides': MANUAL_ABSOLUTE_TOLERANCES,
                                       'purpose': 'API persistence and manual-entry rounding; not physical accuracy'}}
    model = None
    code = 0
    try:
        model = Model.new(str(path), units=definitions.Units.METRIC, overwrite=False)
        model.add(component=definitions.ModelComponents.BLACKOILFLUID, name=context)
        check_units(model, context, definitions)
        for key, value in plan.items():
            model.set_value(context=context, parameter=key, value=value)
        report['before_save'] = inspect_model(model, context, plan, definitions)
        if report['before_save']['comparison']['status'] != 'PASS':
            raise ValueError('Часть назначенных параметров не совпала до сохранения')
        model.save()
        model.close()
        model = None
        model = Model.open(str(path), units=definitions.Units.METRIC)
        report['after_reopen'] = inspect_model(model, context, plan, definitions)
        if report['after_reopen']['comparison']['status'] != 'PASS':
            raise ValueError('Часть параметров не совпала после повторного открытия')
        model.close()
        model = None
        report['model_sha256'] = sha(path)
        if manual:
            model = Model.open(str(manual), units=definitions.Units.METRIC)
            report['manual_model'] = str(manual)
            report['manual_comparison'] = inspect_model(model, context, plan, definitions, manual=True)
        if manual and report['manual_comparison']['comparison']['status'] != 'PASS':
            report['status'] = 'MANUAL_COMPARISON_REQUIRED'
            code = 2
        else:
            report['status'] = 'FLUID_MODEL_VERIFIED'
    except Exception as error:
        report['status'] = 'ERROR'
        report['error'] = '{}: {}'.format(type(error).__name__, error)
        code = 1
    finally:
        if model is not None:
            try:
                model.close()
            except Exception as error:
                report['status'] = 'ERROR'
                report['close_error'] = '{}: {}'.format(type(error).__name__, error)
                code = 1
        with output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print('Fluid model: {}'.format(report['status']))
    if 'manual_comparison' in report:
        print('Manual comparison: {}'.format(report['manual_comparison']['comparison']['status']))
    if 'error' in report:
        print(report['error'])
    print('Отчёт: {}'.format(output))
    return code


if __name__ == '__main__':
    sys.exit(main())
