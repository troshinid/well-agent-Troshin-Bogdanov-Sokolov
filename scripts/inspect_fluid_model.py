"""Create a separate BlackOil probe and inspect live PIPESIM units (3.8+).

Only an empty BlackOil component is added. Source properties are not assigned,
default correlations are not approved, and no hydraulic calculation is run.
"""

import argparse
import inspect
import json
import math
import sys
from pathlib import Path


def json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if hasattr(value, 'item'):
        return json_value(value.item())
    return repr(value)


def error_text(error):
    return '{}: {}'.format(type(error).__name__, error)


def parameter_names(parameters):
    names = {}
    for name in dir(parameters):
        if name.startswith('_'):
            continue
        value = getattr(parameters, name)
        if isinstance(value, str):
            names[name] = value
    return names


def read_parameters(model, context, parameters):
    result = {}
    for label, parameter in parameters.items():
        item = {'parameter': parameter}
        try:
            item['value'] = json_value(model.get_value(context=context, parameter=parameter))
        except Exception as error:
            item['read_error'] = error_text(error)
        try:
            description = model.describe(context=context, parameter=parameter)
            item['description'] = {
                name: json_value(getattr(description, name))
                for name in dir(description)
                if not name.startswith('_')
                and isinstance(getattr(description, name), (str, int, float, bool, type(None)))
            }
        except Exception as error:
            item['description_error'] = error_text(error)
        result[label] = item
    return result


def settings_api(model, definitions):
    """Describe the settings interface without calling unfamiliar methods."""
    result = {}
    cls = getattr(definitions.Parameters, 'SimulationSetting', None)
    if cls is not None:
        result['parameter_names'] = parameter_names(cls)
    settings = model.sim_settings
    result['type'] = type(settings).__name__
    result['public_names'] = [name for name in dir(settings) if not name.startswith('_')]
    result['members'] = {}
    for name in result['public_names']:
        try:
            member = getattr(settings, name)
            if callable(member):
                try:
                    signature = str(inspect.signature(member))
                except (TypeError, ValueError):
                    signature = None
                result['members'][name] = {
                    'signature': signature, 'doc': (inspect.getdoc(member) or '')[:6000],
                }
            elif isinstance(member, (str, int, float, bool, type(None))):
                result['members'][name] = json_value(member)
        except Exception as error:
            result['members'][name] = {'inspection_error': error_text(error)}
    return result


def compare_readback(before, after):
    differences = {}
    checked = 0
    for label, item in before.items():
        saved = after[label]
        if 'read_error' in item or 'read_error' in saved:
            differences[label] = {'before': item, 'after': saved}
            continue
        a, b = item['value'], saved['value']
        if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            # Numerical readback tolerance only; this is not hydraulic accuracy.
            equal = math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12)
        else:
            equal = type(a) is type(b) and a == b
        checked += 1
        if not equal:
            differences[label] = {'before': a, 'after': b}
    return {'status': 'PASS' if checked and not differences else 'CHECK_REQUIRED',
            'checked': checked, 'differences': differences,
            'numeric_tolerance': {'relative': 1e-10, 'absolute': 1e-12}}


def main():
    parser = argparse.ArgumentParser(description='Черновик BlackOil и единицы живой модели PIPESIM')
    parser.add_argument('model', type=Path, help='Новый файл fluid_probe.pips вне репозитория')
    args = parser.parse_args()
    path = args.model.resolve()
    report_path = path.with_suffix('.json')
    if path.suffix.lower() != '.pips':
        parser.error('Укажите файл с расширением .pips')
    if path.exists() or report_path.exists():
        parser.error('Файл уже существует. Укажите другое имя, например fluid_probe_2.pips')
    try:
        import sixgill.definitions as definitions
        from sixgill.pipesim import Model
    except ImportError as error:
        parser.error('Нужно активированное окружение Toolkit: {}'.format(error))

    path.parent.mkdir(parents=True, exist_ok=True)
    context = 'BLACKOIL_API_PROBE'
    params = parameter_names(definitions.Parameters.BlackOilFluid)
    calibration = definitions.Parameters.BlackOilFluid.SinglePointCalibration
    params.update({'SinglePointCalibration.' + key: value
                   for key, value in parameter_names(calibration).items()})
    report = {'purpose': 'BlackOil default units and save/reopen inspection',
              'python_version': sys.version, 'model_file': str(path),
              'component': context, 'source_properties_assigned': False,
              'calculation_ready': False, 'hydraulic_calculation_run': False,
              'note': 'Все прочитанные значения — настройки нового компонента по умолчанию.',
              'status': 'IN_PROGRESS'}
    model = None
    exit_code = 0
    try:
        model = Model.new(str(path), units=definitions.Units.METRIC, overwrite=False)
        model.add(component=definitions.ModelComponents.BLACKOILFLUID, name=context)
        report['metric_before_save'] = read_parameters(model, context, params)
        try:
            report['simulation_settings_api'] = settings_api(model, definitions)
        except Exception as error:
            report['simulation_settings_inspection_error'] = error_text(error)
        model.save()
        model.close()
        model = None
        model = Model.open(str(path), units=definitions.Units.METRIC)
        report['metric_after_reopen'] = read_parameters(model, context, params)
        report['readback'] = compare_readback(report['metric_before_save'], report['metric_after_reopen'])
        model.close()
        model = None
        model = Model.open(str(path), units=definitions.Units.SI)
        report['si_after_reopen'] = read_parameters(model, context, params)
        report['status'] = 'INSPECTION_COMPLETE'
    except Exception as error:
        report['status'] = 'ERROR'
        report['error'] = error_text(error)
        exit_code = 1
    finally:
        if model is not None:
            try:
                model.close()
            except Exception as error:
                report['close_error'] = error_text(error)
                report['status'] = 'ERROR'
                exit_code = 1
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('Inspection: {}'.format(report['status']))
    if 'readback' in report:
        print('Save/reopen: {}'.format(report['readback']['status']))
    if 'error' in report:
        print(report['error'])
    print('Отчёт: {}'.format(report_path))
    print('Исходные свойства ещё не назначены; расчёт не запускался.')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
