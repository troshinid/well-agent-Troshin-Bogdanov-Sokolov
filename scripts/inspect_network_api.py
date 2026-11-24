"""Inspect installed network APIs using a temporary copy of a model (Py3.8+).

Does not call Model.new/add/connect/set_value/save or any simulation method.
Task proxies are inspected, never run. The user's source .pips is not opened.
"""

import argparse
import hashlib
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path


PARAMETER_CLASSES = [
    'Source', 'Sink', 'Flowline', 'FlowlineGeometry', 'Boundary', 'Location',
    'SurfaceComponent', 'DownholeOrSurfaceComponent', 'SchematicPosition',
    'AssociatedToFluid', 'FluidLink', 'FlowCorrelation', 'HeatTransferOption',
    'SimulationSetting', 'NetworkSimulation', 'PTProfileSimulation',
    'SingleBranchSimulation', 'AbstractTask', 'Study',
]
CONSTANT_CLASSES = [
    'FlowRateCondition', 'FlowRateType', 'FluidType', 'GaugeType',
    'PressureGradientType', 'ReferenceDepthOption', 'Orientation',
    'ProfilePlotOption', 'AmbientTemperatureOption', 'PipeHeatTransfer',
    'HeatTransferCoefficient', 'InsideFilmCoeffMethod', 'UValueInputOption',
    'SinglePhaseFlowCorrelation', 'MultiphaseFlowCorrelationSource',
    'MultiphaseFlowCorrelation', 'NetworkSolverMethod', 'RangeType',
    'CalculatedVariable', 'SystemVarType',
]
MODEL_METHODS = [
    'new', 'open', 'add', 'connect', 'disconnect', 'connections',
    'get_connections', 'find', 'find_components', 'get_geometry', 'set_geometry',
    'validate', 'describe', 'get_value', 'set_value', 'get_values', 'set_values',
    'save', 'close',
]


def names(obj):
    return [name for name in dir(obj) if not name.startswith('_')]


def callable_info(obj):
    try:
        signature = str(inspect.signature(obj))
    except (TypeError, ValueError):
        signature = None
    return {'signature': signature, 'doc': inspect.getdoc(obj)}


def definitions_snapshot(obj, depth=0):
    result = {}
    for name in names(obj):
        value = getattr(obj, name)
        if value is None or isinstance(value, (str, bool, int, float)):
            result[name] = value
        elif inspect.isclass(value) and depth < 2:
            result[name] = definitions_snapshot(value, depth + 1)
    return result


def source_info(obj, limit=35000):
    try:
        text = inspect.getsource(obj)
        return {'text': text[:limit], 'truncated': len(text) > limit}
    except (TypeError, OSError):
        return {'available': False}


def object_api(obj):
    result = {'type': type(obj).__name__, 'module': type(obj).__module__,
              'public_names': names(obj), 'methods': {}, 'inspection_errors': {}}
    for name in result['public_names']:
        try:
            member = getattr(obj, name)
            if callable(member):
                result['methods'][name] = callable_info(member)
                if name in {'run', 'get_conditions', 'set_conditions', 'get_boundaries',
                            'set_boundaries', 'get_default_conditions', 'validate'}:
                    result['methods'][name]['source'] = source_info(member, limit=20000)
        except Exception as error:
            result['inspection_errors'][name] = '{}: {}'.format(type(error).__name__, error)
    result['class_source'] = source_info(type(obj))
    return result


def build_report(definitions, model_class, model):
    report = {
        'python_version': sys.version, 'python_executable': sys.executable,
        'toolkit_version_definition': str(getattr(definitions, 'PYTHON_TOOLKIT_VERSION', 'unknown')),
        'definitions_file': getattr(definitions, '__file__', None),
        'model_methods': {name: callable_info(getattr(model_class, name))
                          for name in MODEL_METHODS if hasattr(model_class, name)},
        'parameters': {name: definitions_snapshot(getattr(definitions.Parameters, name))
                       for name in PARAMETER_CLASSES if hasattr(definitions.Parameters, name)},
        'constants': {name: definitions_snapshot(getattr(definitions.Constants, name))
                      for name in CONSTANT_CLASSES if hasattr(definitions.Constants, name)},
        'other_definitions': {name: definitions_snapshot(getattr(definitions, name))
                              for name in ['ModelComponents', 'Units', 'Tasks', 'SimulationOptions',
                                           'SystemVariables', 'ProfileVariables']
                              if hasattr(definitions, name)},
        'task_api': {},
    }
    for name in ['get_geometry', 'set_geometry', 'connect', 'get_connections', 'validate']:
        if hasattr(model_class, name):
            report['model_methods'][name]['source'] = source_info(getattr(model_class, name), limit=20000)
    tasks = model.tasks
    report['task_factory'] = object_api(tasks)
    for name in names(tasks):
        if not any(part in name.lower() for part in ['network', 'ptprofile']):
            continue
        try:
            task = getattr(tasks, name)
            report['task_api'][name] = object_api(task)
        except Exception as error:
            report['task_api'][name] = {'inspection_error': '{}: {}'.format(type(error).__name__, error)}
    report['simulation_settings_api'] = object_api(model.sim_settings)
    return report


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description='Описание API трубы, соединений и сетевого расчёта')
    parser.add_argument('model', type=Path, help='Существующая t1_fluid_auto.pips')
    parser.add_argument('output', type=Path, help='Новый diagnostics/network_api.json')
    args = parser.parse_args()
    source, output = args.model.resolve(), args.output.resolve()
    if not source.is_file() or source.suffix.lower() != '.pips':
        parser.error('Укажите существующую модель .pips')
    if output.exists() or output.suffix.lower() != '.json':
        parser.error('Укажите новое имя отчёта .json')
    try:
        import sixgill.definitions as definitions
        from sixgill.pipesim import Model
    except ImportError as error:
        parser.error('Нужно активированное окружение Toolkit: {}'.format(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    before = digest(source)
    report = {'status': 'IN_PROGRESS', 'source_model': str(source),
              'source_model_sha256_before': before, 'model_saved': False,
              'hydraulic_calculation_run': False, 'inspection_uses_temporary_copy': True}
    model = None
    code = 0
    try:
        with tempfile.TemporaryDirectory(prefix='network_api_', dir=str(output.parent)) as temp:
            copy = Path(temp) / 'inspection_copy.pips'
            shutil.copyfile(str(source), str(copy))
            try:
                model = Model.open(str(copy), units=definitions.Units.METRIC)
                report.update(build_report(definitions, Model, model))
            finally:
                if model is not None:
                    model.close()
                    model = None
        report['status'] = 'INSPECTION_COMPLETE'
    except Exception as error:
        report['status'] = 'ERROR'
        report['error'] = '{}: {}'.format(type(error).__name__, error)
        code = 1
    after = digest(source)
    report['source_model_sha256_after'] = after
    report['source_model_unchanged'] = before == after
    if before != after:
        report['status'] = 'ERROR'
        report['error'] = 'Контрольная сумма исходной модели изменилась во время проверки'
        code = 1
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print('Network API inspection: {}'.format(report['status']))
    print('Исходная модель: {}'.format('UNCHANGED' if before == after else 'CHANGED'))
    print('Расчёт не запускался. Отчёт: {}'.format(output))
    return code


if __name__ == '__main__':
    sys.exit(main())
