"""Read-only inspection of the installed PIPESIM BlackOil API (Python 3.8+)."""

import argparse
import inspect
import json
import math
import re
import sys
from pathlib import Path


RELEVANT = re.compile(
    r'black|oil|fluid|viscos|emuls|gas|water|mix|calib|density|correlation|standard|stock|surface',
    re.IGNORECASE,
)


def public_names(value):
    return [name for name in dir(value) if not name.startswith('_')]


def describe_callable(value):
    try:
        signature = str(inspect.signature(value))
    except (TypeError, ValueError):
        signature = None
    return {'signature': signature, 'doc': inspect.getdoc(value)}


def snapshot(value, depth=0, seen=None, budget=None):
    """Inspect public definitions without constructing SDK objects."""
    if seen is None:
        seen = set()
    if budget is None:
        budget = [3000]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if budget[0] <= 0:
        return {'truncated': 'entry limit'}
    budget[0] -= 1
    if isinstance(value, (list, tuple, set, frozenset)):
        return [snapshot(item, depth + 1, seen, budget) for item in value]
    if isinstance(value, dict):
        return {str(key): snapshot(item, depth + 1, seen, budget) for key, item in value.items()}
    if inspect.isclass(value) or inspect.ismodule(value):
        if id(value) in seen:
            return {'type': getattr(value, '__name__', str(type(value))), 'repeated': True}
        seen.add(id(value))
        names = public_names(value)
        result = {'name': getattr(value, '__name__', None), 'doc': inspect.getdoc(value), 'names': names}
        if depth >= 5:
            result['truncated'] = 'depth limit'
            return result
        members = {}
        for name in names:
            if budget[0] <= 0:
                result['truncated'] = 'entry limit'
                break
            try:
                members[name] = snapshot(getattr(value, name), depth + 1, seen, budget)
            except Exception as error:
                members[name] = {'inspection_error': '{}: {}'.format(type(error).__name__, error)}
        result['members'] = members
        return result
    if callable(value):
        return describe_callable(value)
    return {'type': type(value).__name__, 'repr': repr(value)}


def source_text(value):
    try:
        text = inspect.getsource(value)
        return {'text': text[:120000], 'truncated': len(text) > 120000}
    except (OSError, TypeError):
        return {'available': False}


def build_report(sixgill, definitions, model_class):
    report = {
        'inspection_type': 'read_only_api_metadata',
        'python_version': sys.version,
        'python_executable': sys.executable,
        'sixgill_file': getattr(sixgill, '__file__', None),
        'definitions_file': getattr(definitions, '__file__', None),
        'definitions_names': public_names(definitions),
        'model_public_names': public_names(model_class),
        'model_methods': {},
        'model_instances_created': 0,
    }
    methods = {'new', 'open', 'add', 'save', 'close', 'set_value', 'get_value', 'set_values', 'get_values'}
    methods.update(name for name in report['model_public_names']
                   if RELEVANT.search(name) or re.search(r'describ|param|unit', name, re.IGNORECASE))
    for name in sorted(methods):
        if hasattr(model_class, name):
            report['model_methods'][name] = describe_callable(getattr(model_class, name))

    parameters = getattr(definitions, 'Parameters', None)
    if parameters is None:
        report['parameters_error'] = 'Parameters is absent'
    else:
        report['parameters_names'] = public_names(parameters)
        blackoil = getattr(parameters, 'BlackOilFluid', None)
        if blackoil is not None:
            report['blackoil_parameters'] = snapshot(blackoil)
            report['blackoil_parameters_source'] = source_text(blackoil)
        else:
            report['parameters_error'] = 'Parameters.BlackOilFluid is absent'
        report['other_fluid_parameters'] = {
            name: snapshot(getattr(parameters, name))
            for name in public_names(parameters) if name != 'BlackOilFluid' and RELEVANT.search(name)
        }

    constants = getattr(definitions, 'Constants', None)
    if constants is not None:
        report['constants_names'] = public_names(constants)
        report['fluid_constants'] = {
            name: snapshot(getattr(constants, name))
            for name in public_names(constants) if RELEVANT.search(name)
        }
    report['additional_definitions'] = {
        name: snapshot(getattr(definitions, name))
        for name in public_names(definitions)
        if name in {'Units', 'ModelComponents'} or RELEVANT.search(name)
    }
    return report


def main():
    parser = argparse.ArgumentParser(description='Описание установленного API флюида PIPESIM')
    parser.add_argument('output', type=Path, help='Путь к fluid_api.json вне репозитория')
    args = parser.parse_args()
    try:
        import sixgill
        import sixgill.definitions as definitions
        from sixgill.pipesim import Model
    except ImportError as error:
        parser.error('Запустите скрипт в активированном окружении Toolkit: {}'.format(error))
    report = build_report(sixgill, definitions, Model)
    output = args.output.resolve()
    if output.suffix.lower() != '.json':
        parser.error('Укажите выходной файл с расширением .json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('API inspection: OK')
    print('Экземпляры модели не создавались')
    print('Отчёт: {}'.format(output))


if __name__ == '__main__':
    main()
