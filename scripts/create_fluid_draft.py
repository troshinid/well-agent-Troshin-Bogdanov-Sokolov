"""Assign verified BlackOil properties and check persistence (Python 3.8+).

Run with the activated PIPESIM Toolkit. This creates a NEW draft: density,
phase ratios, PVT correlations and mixture viscosity still need configuration.
"""

import argparse
import json
import math
import sys
from pathlib import Path

from check_fluid import check_fluid


def parameters_from_data(checked, definitions):
    points = checked['oil_properties']
    if len(points) != 2:
        raise ValueError('User2Point требует ровно две температурные точки')
    p = definitions.Parameters.BlackOilFluid
    # API units confirmed by the user's live 2021.1 fluid_probe.json:
    # METRIC temperature: degC; dynamic viscosity: kg/(m.s) = Pa.s.
    return {
        p.DEADOILTEMPERATURE1: float(points[0]['temperature_c']),
        p.DEADOILTEMPERATURE2: float(points[1]['temperature_c']),
        p.DEADOILVISCOSITY1: float(points[0]['dynamic_viscosity_mpa_s']) / 1000,
        p.DEADOILVISCOSITY2: float(points[1]['dynamic_viscosity_mpa_s']) / 1000,
        p.DEADOILVISCOSITYCORR: definitions.Constants.DeadOilViscosityCorrelation.USER2POINT,
        p.GASSPECIFICGRAVITY: float(checked['gas_relative_density_to_air_ideal']),
        p.WATERSPECIFICGRAVITY: 1.0,  # Declared pure-water scenario assumption.
    }


def check_units(model, context, definitions):
    p = definitions.Parameters.BlackOilFluid
    expected = {
        p.DEADOILTEMPERATURE1: 'degC', p.DEADOILTEMPERATURE2: 'degC',
        p.DEADOILVISCOSITY1: 'kg/(m.s)', p.DEADOILVISCOSITY2: 'kg/(m.s)',
        p.GASSPECIFICGRAVITY: '', p.WATERSPECIFICGRAVITY: '',
    }
    actual = {}
    for parameter, unit in expected.items():
        description = model.describe(context=context, parameter=parameter)
        symbol = (description.units_symbol or '').strip()
        if symbol != unit:
            raise ValueError('{}: ожидались единицы {!r}, получены {!r}'.format(parameter, unit, symbol))
        if not unit and description.units != 'dimensionless':
            raise ValueError('{}: ожидалась безразмерная величина'.format(parameter))
        actual[parameter] = {'units': description.units, 'units_symbol': description.units_symbol}
    return actual


def verify(model, context, expected):
    result = {}
    for parameter, value in expected.items():
        actual = model.get_value(context=context, parameter=parameter)
        if isinstance(value, str):
            matches = actual == value
        else:
            matches = (not isinstance(actual, bool) and isinstance(actual, (int, float))
                       and math.isfinite(actual)
                       and math.isclose(actual, value, rel_tol=1e-8, abs_tol=1e-12))
        result[parameter] = {'expected': value,
                             'actual': actual if isinstance(actual, (str, bool, int, float)) else repr(actual),
                             'matches': matches}
        if not matches:
            raise ValueError('{}: ожидалось {!r}, прочитано {!r}'.format(parameter, value, actual))
    return result


def main():
    parser = argparse.ArgumentParser(description='Первое назначение свойств флюида в PIPESIM')
    parser.add_argument('fluid', type=Path, help='Исходный fluid.json')
    parser.add_argument('model', type=Path, help='НОВЫЙ файл t1_fluid_draft.pips')
    args = parser.parse_args()
    fluid = args.fluid.resolve()
    path = args.model.resolve()
    report_path = path.with_suffix('.json')
    if path.suffix.lower() != '.pips':
        parser.error('Модель должна иметь расширение .pips')
    if path.exists() or report_path.exists():
        parser.error('Выходной файл уже существует. Укажите другое имя модели.')
    try:
        checked = check_fluid(fluid)
        import sixgill.definitions as definitions
        from sixgill.pipesim import Model
        expected = parameters_from_data(checked, definitions)
    except (OSError, ValueError, ImportError, AttributeError) as error:
        parser.error(str(error))

    report = {
        'status': 'IN_PROGRESS', 'fluid_id': checked['fluid_id'],
        'model_file': str(path), 'fluid_config_sha256': checked['fluid_config_sha256'],
        'source_pdf_sha256': checked['source_sha256'], 'assigned_parameters': expected,
        'gas_sg_basis': {'method': 'ideal_molar_mass_ratio',
                         'air_molar_mass_g_mol': str(checked['assumed_air_molar_mass_g_mol'])},
        'water_sg_basis': 'scenario_assumption: pure water, specific gravity 1.0',
        'calculation_ready': False, 'hydraulic_calculation_run': False,
        'unapproved_defaults': ['oil_density', 'phase_ratios', 'pvt_correlations', 'mixture_viscosity'],
        'pending': ['Сверить условия учёта объёмов в PIPESIM и сценарии',
                    'Задать плотность нефти при согласованной опорной температуре',
                    'Задать GOR и обводнённость при согласованных условиях',
                    'Явно выбрать корреляции PVT и правило вязкости смеси'],
        'readback_tolerance': {'relative': 1e-8, 'absolute': 1e-12,
                               'purpose': 'API persistence only, not hydraulic accuracy'},
    }
    model = None
    exit_code = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        model = Model.new(str(path), units=definitions.Units.METRIC, overwrite=False)
        context = checked['fluid_id']
        model.add(component=definitions.ModelComponents.BLACKOILFLUID, name=context)
        report['units'] = check_units(model, context, definitions)
        for parameter, value in expected.items():
            model.set_value(context=context, parameter=parameter, value=value)
        report['before_save'] = verify(model, context, expected)
        model.save()
        model.close()
        model = None
        model = Model.open(str(path), units=definitions.Units.METRIC)
        report['units_after_reopen'] = check_units(model, context, definitions)
        report['after_reopen'] = verify(model, context, expected)
        report['status'] = 'ASSIGNED_PROPERTIES_VERIFIED'
    except Exception as error:
        report['status'] = 'ERROR'
        report['error'] = '{}: {}'.format(type(error).__name__, error)
        exit_code = 1
    finally:
        if model is not None:
            try:
                model.close()
            except Exception as error:
                report['close_error'] = '{}: {}'.format(type(error).__name__, error)
                report['status'] = 'ERROR'
                exit_code = 1
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('Fluid setup: {}'.format(report['status']))
    if 'error' in report:
        print(report['error'])
    print('Модель: {}'.format(path))
    print('Отчёт: {}'.format(report_path))
    print('Черновик: плотность нефти, фазовые соотношения и остальные корреляции ещё не настроены.')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
