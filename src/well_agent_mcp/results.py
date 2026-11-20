"""Local registration, integrity checks, report projection and per-call traces."""

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import ValidationError

from .schemas import (
    Comparison, Difference, Error, LiquidRate, Pressure, Profile, ReadRequest,
    ReadResponse, ReportRef, ScenarioResult, StockConditions, Temperature, WarningMessage,
)

SOURCE, SINK, ROUTE = 'agzu6_out', 'tiein_agzu4_agzu1', 'agzu6_to_tiein'
PASS = {'NUMERICAL_CHECKS_PASSED', 'NUMERICAL_CHECKS_PASSED_WITH_WARNINGS'}
REPORT_STATES = PASS | {'CHECK_REQUIRED', 'ERROR', 'IN_PROGRESS'}
REQUIRED_CHECKS = {
    'completed', 'summary_available', 'no_solver_errors', 'messages_available',
    'result_properties_available', 'positive_absolute_pressures', 'outlet_pressure',
    'node_temperatures', 'liquid_rate', 'liquid_balance', 'oil_rate', 'oil_balance',
    'water_rate', 'water_balance', 'gas_rate', 'gas_balance', 'profile_columns_aligned',
    'profile_point_count', 'isothermal_profile', 'positive_profile_pressures',
    'profile_end_pressures', 'profile_distance_monotonic', 'profile_length', 'profile_rise',
}


class ReportError(Exception):
    def __init__(self, code, field, message):
        self.payload = Error(code=code, field=field, message=message)
        super().__init__(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def parse_json(data, field):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON number')
    try:
        value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError('Expected object')
        return value
    except (ValueError, UnicodeError) as error:
        raise ReportError('REPORT_INVALID', field, 'Некорректный JSON: ' + field) from error


def read_bytes(path, field):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError as error:
        code = 'REPORT_NOT_FOUND' if field == 'report' else 'INPUT_MISMATCH'
        raise ReportError(code, field, 'Зарегистрированный файл отсутствует: ' + field) from error
    except OSError as error:
        raise ReportError('REPORT_INVALID', field, 'Не удалось прочитать файл: ' + field) from error


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def stock_conditions(rates):
    try:
        raw = rates['stock_tank_conditions']
        t, p = float(raw['temperature_c']), float(raw['pressure_pa'])
        if raw['pressure_scale'] != 'absolute' or not math.isfinite(t) or not math.isfinite(p) or p <= 0:
            raise ValueError('Invalid reference conditions')
        return StockConditions(temperature=Temperature(object_id='stock_tank', value=t),
                               pressure=Pressure(object_id='stock_tank', value=p / 100000))
    except (KeyError, TypeError, ValueError) as error:
        raise ReportError('REPORT_INVALID', 'rates.stock_tank_conditions', 'Не заданы условия учёта объёмов') from error


def validate_origin(report, manifest, model_hash, manifest_hash, rates_hash):
    if (report.get('source_sha256') != model_hash or report.get('manifest_sha256') != manifest_hash
            or report.get('rates_sha256') != rates_hash or report.get('source_model_unchanged') is not True):
        raise ReportError('INPUT_MISMATCH', 'input_hashes', 'Отчёт не соответствует исходным файлам')
    if (manifest.get('status') != 'NETWORK_STRUCTURE_VERIFIED' or manifest.get('model_sha256') != model_hash
            or manifest.get('input_hashes', {}).get('rates_json') != rates_hash):
        raise ReportError('INPUT_MISMATCH', 'manifest', 'Нет подтверждённой сборки исходной модели')
    connections = manifest.get('after_reopen', {}).get('connections', [])
    if not isinstance(connections, list) or any(not isinstance(edge, dict) or
            not isinstance(edge.get('Source'), str) or not isinstance(edge.get('Destination'), str)
            for edge in connections):
        raise ReportError('REPORT_INVALID', 'manifest.connections', 'Неизвестный формат связей')
    edges = {(edge['Source'], edge['Destination']) for edge in connections}
    if len(connections) != 2 or edges != {(SOURCE, ROUTE), (ROUTE, SINK)}:
        raise ReportError('INPUT_MISMATCH', 'route_id', 'Этот инструмент поддерживает участок agzu6_to_tiein')
    if report.get('status') not in REPORT_STATES or not isinstance(report.get('studies'), dict):
        raise ReportError('REPORT_INVALID', 'report.status', 'Неизвестный формат отчёта run_network_model')
    if report.get('physical_validation') != 'NOT_ASSESSED':
        raise ReportError('REPORT_INVALID', 'physical_validation', 'Промысловая точность этой модели не подтверждена')
    if report['status'] in PASS:
        try:
            all_runs = [report['studies'][name]['runs'][stage]
                        for name in ['base', 'plus10'] for stage in ['run', 'restart']]
            consistent = (report.get('stage') == 'complete' and all(run['status'] in PASS for run in all_runs)
                          and all(report['studies'][name]['repeatability']['matches'] is True
                                  for name in ['base', 'plus10']))
            warned = any(run['warnings'] for run in all_runs)
            consistent = consistent and (warned == (report['status'] == 'NUMERICAL_CHECKS_PASSED_WITH_WARNINGS'))
        except (KeyError, TypeError):
            consistent = False
        if not consistent:
            raise ReportError('REPORT_INVALID', 'report.status', 'Общий статус PASS противоречит этапам расчёта')


def write_json(path, payload):
    """Owned registry/trace files only; never writes to source model or report."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(uuid4()) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def register(self, report_path, model_path, rates_path):
        paths = {'report': Path(report_path).resolve(), 'source_model': Path(model_path).resolve(),
                 'rates': Path(rates_path).resolve(), 'manifest': Path(model_path).resolve().with_suffix('.json')}
        content = {name: read_bytes(path, name) for name, path in paths.items()}
        report = parse_json(content['report'], 'report')
        manifest = parse_json(content['manifest'], 'manifest')
        rates = parse_json(content['rates'], 'rates')
        hashes = {name: checksum(data) for name, data in content.items()}
        validate_origin(report, manifest, hashes['source_model'], hashes['manifest'], hashes['rates'])
        stock_conditions(rates)
        # Same file/version is registered under the same ID on repeated calls.
        run_id = str(uuid5(NAMESPACE_URL, paths['report'].as_uri() + '#' + hashes['report']))
        entry = {'schema_version': 1, 'run_id': run_id, 'route_id': ROUTE,
                 'files': {name: {'path': str(path), 'sha256': hashes[name]} for name, path in paths.items()}}
        self.directory.mkdir(parents=True, exist_ok=True)
        entry_path = self.directory / (run_id + '.json')
        if entry_path.exists():
            if parse_json(read_bytes(entry_path, 'registry'), 'registry') != entry:
                raise ReportError('INPUT_MISMATCH', 'registry', 'У run_id уже другая регистрация')
        else:
            write_json(entry_path, entry)
        return run_id

    def read(self, run_id):
        entry_path = self.directory / (run_id + '.json')
        if not entry_path.is_file():
            raise ReportError('RUN_NOT_FOUND', 'run_id', 'Расчёт не зарегистрирован')
        entry = parse_json(read_bytes(entry_path, 'registry'), 'registry')
        if entry.get('schema_version') != 1 or entry.get('run_id') != run_id or entry.get('route_id') != ROUTE:
            raise ReportError('REPORT_INVALID', 'registry', 'Неизвестный формат записи реестра')
        try:
            files = entry['files']
            content = {}
            for name in ['report', 'source_model', 'rates', 'manifest']:
                content[name] = read_bytes(files[name]['path'], name)
                if checksum(content[name]) != files[name]['sha256']:
                    raise ReportError('INPUT_MISMATCH', name, 'После регистрации изменён файл: ' + name)
            report = parse_json(content['report'], 'report')
            rates = parse_json(content['rates'], 'rates')
            validate_origin(report, parse_json(content['manifest'], 'manifest'), files['source_model']['sha256'],
                            files['manifest']['sha256'], files['rates']['sha256'])
            return report, rates
        except (KeyError, TypeError) as error:
            raise ReportError('REPORT_INVALID', 'registry', 'Повреждена запись реестра') from error


def _scenario(study, name, repeat_limit):
    """Return available values even when the scenario is incomplete."""
    runs = study.get('runs', {})
    if not isinstance(runs, dict):
        raise ReportError('REPORT_INVALID', name + '.runs', 'Неизвестный формат этапов расчёта')
    available = [stage for stage in ['restart', 'run'] if isinstance(runs.get(stage), dict)]
    completed = [stage for stage in available if runs[stage].get('checks', {}).get('completed') is True]
    stage = next((s for s in completed if runs[s].get('status') in PASS), None)
    stage = stage or next(iter(completed), None) or next(iter(available), None)
    if stage is None:
        return None, [], False
    raw = runs[stage]
    checks = raw.get('checks', {})
    if not isinstance(checks, dict) or any(type(v) is not bool for v in checks.values()):
        raise ReportError('REPORT_INVALID', name + '.checks', 'Проверки должны содержать логические значения')
    warnings = []
    for label in ['run', 'restart']:
        other = runs.get(label, {})
        if not isinstance(other, dict):
            raise ReportError('REPORT_INVALID', name + '.runs', 'Неизвестный формат этапа')
        messages = other.get('warnings', [])
        if not isinstance(messages, list) or any(not isinstance(m, str) for m in messages):
            raise ReportError('REPORT_INVALID', name + '.warnings', 'Неизвестный формат предупреждений')
        warnings.extend(WarningMessage(scenario=name, stage=label, message=m) for m in messages)
        if other.get('status') == 'NUMERICAL_CHECKS_PASSED' and messages:
            raise ReportError('REPORT_INVALID', name + '.warnings', 'Предупреждения противоречат статусу этапа')
    if raw.get('status') in PASS and (not REQUIRED_CHECKS <= checks.keys() or not all(checks.values())):
        raise ReportError('REPORT_INVALID', name + '.checks', 'Статус PASS противоречит численным проверкам')
    profile = raw.get('profile') or {}
    liquid = (raw.get('stock_rates_m3_day') or {}).get('liquid') or {}
    points = profile.get('points')
    if type(points) is not int or points <= 0:
        points = None
    pin = number(raw.get('inlet_pressure_pa_absolute'))
    pout = number(raw.get('outlet_pressure_pa_absolute'))
    vals = [pin, pout, number(liquid.get('source')), number(liquid.get('sink')),
            number(profile.get('temperature_min_c')), number(profile.get('temperature_max_c'))]
    complete = all(v is not None for v in vals) and points is not None
    if complete and vals[4] > vals[5]:
        raise ReportError('REPORT_INVALID', name + '.profile', 'Минимальная температура больше максимальной')
    # These labels only describe checks already performed by the hydraulic runner.
    numerical = ('OK' if raw.get('status') in PASS and complete else
                 'OUT_OF_TOLERANCE' if any(v is False for v in checks.values()) else 'NOT_ASSESSED')
    repetition = study.get('repeatability', {})
    repeatability = 'FAILED' if repetition.get('matches') is False else 'NOT_ASSESSED'
    if repetition.get('matches') is True and {'run', 'restart'} <= set(completed) and complete:
        first = number(runs['run'].get('inlet_pressure_pa_absolute'))
        second = number(runs['restart'].get('inlet_pressure_pa_absolute'))
        recorded = number(repetition.get('restart_minus_run_inlet_pressure_pa'))
        if (first is None or second is None or recorded is None or
                abs((second - first) - recorded) > 1e-6 or abs(second - first) > repeat_limit):
            raise ReportError('REPORT_INVALID', name + '.repeatability', 'Значения давлений противоречат проверке повторяемости')
        repeatability = 'CONFIRMED'
    if repeatability == 'FAILED':
        numerical = 'OUT_OF_TOLERANCE'
    result = ScenarioResult(
        result_origin=stage, execution_status='SUCCESS' if checks.get('completed') is True else 'FAILED',
        numerical_validation=numerical,
        inlet_pressure=Pressure(object_id=SOURCE, value=None if pin is None else pin / 1e5),
        outlet_pressure=Pressure(object_id=SINK, value=None if pout is None else pout / 1e5),
        liquid_rate_source=LiquidRate(object_id=SOURCE, value=vals[2]),
        liquid_rate_sink=LiquidRate(object_id=SINK, value=vals[3]),
        profile=Profile(points=points, temperature_min=Temperature(object_id=ROUTE, value=vals[4]),
                        temperature_max=Temperature(object_id=ROUTE, value=vals[5])),
        repeatability=repeatability, checks=checks)
    return result, warnings, complete


def project(report, rates, request, response):
    requested = ['base', 'plus10'] if request.scenario == 'compare' else [request.scenario]
    response.report_ref = ReportRef(run_id=request.run_id)
    response.stock_tank_conditions = stock_conditions(rates)
    limits = report.get('numerical_limits', {})
    if (not isinstance(limits, dict) or not {'outlet_pressure_pa', 'temperature_k', 'rate_relative',
            'run_restart_pressure_pa'} <= limits.keys() or any(number(v) is None or v < 0 for v in limits.values())):
        raise ReportError('REPORT_INVALID', 'numerical_limits', 'Некорректные численные допуски отчёта')
    response.numerical_limits = {k: float(v) for k, v in limits.items()}
    complete = []
    for name in requested:
        study = report['studies'].get(name, {})
        if not isinstance(study, dict):
            raise ReportError('REPORT_INVALID', name, 'Неизвестный формат сценария')
        value, warnings, readable = _scenario(study, name, limits['run_restart_pressure_pa'])
        response.warnings.extend(warnings)
        if value is not None:
            response.results[name] = value
        complete.append(readable)
    completed = [value.execution_status == 'SUCCESS' for value in response.results.values()]
    response.execution_status = ('SUCCESS' if len(completed) == len(requested) and all(completed) else
                                  'PARTIAL' if any(completed) else 'FAILED' if completed else 'UNKNOWN')
    response.numerical_validation = (
        'OUT_OF_TOLERANCE' if any(v.numerical_validation == 'OUT_OF_TOLERANCE' for v in response.results.values()) else
        'OK' if len(completed) == len(requested) and all(complete) and
                all(v.numerical_validation == 'OK' for v in response.results.values()) else 'NOT_ASSESSED')
    if report.get('input_mismatch') or report.get('source_model_unchanged') is not True:
        response.numerical_validation = 'OUT_OF_TOLERANCE'
        for scenario in response.results.values():
            scenario.numerical_validation = 'NOT_ASSESSED'
        raise ReportError('INPUT_MISMATCH', 'report.input_mismatch', 'Отчёт содержит ошибку проверки входных параметров')
    if report['status'] not in PASS:
        if response.numerical_validation == 'OK':
            response.numerical_validation = 'NOT_ASSESSED'
        code = 'RESULT_INCOMPLETE' if response.execution_status in ['PARTIAL', 'UNKNOWN'] else 'CALCULATION_FAILED'
        raise ReportError(code, 'report.status', 'Расчётный скрипт не завершил все проверки; доступные результаты сохранены')
    if response.execution_status != 'SUCCESS' or not all(complete):
        raise ReportError('RESULT_INCOMPLETE', 'results', 'Нет обязательных результатов запрошенных сценариев')
    if response.numerical_validation != 'OK':
        raise ReportError('CALCULATION_FAILED', 'checks', 'Численные проверки не пройдены')
    if request.scenario == 'compare':
        base = response.results['base'].inlet_pressure.value
        plus = response.results['plus10'].inlet_pressure.value
        response.comparison = Comparison(inlet_pressure_plus10_minus_base=Difference(value=plus - base))
    response.tool_status = 'SUCCESS'


class ReadResults:
    def __init__(self, registry):
        self.registry = Registry(registry)

    def call(self, arguments):
        started = utc_now()
        response = ReadResponse(trace_id=str(uuid4()), run_id=None, scenario=None,
                                tool_status='ERROR', execution_status='UNKNOWN', numerical_validation='NOT_ASSESSED',
                                results={}, comparison=None, warnings=[], report_ref=None,
                                stock_tank_conditions=None, numerical_limits={}, error=None)
        try:
            try:
                request = ReadRequest.model_validate(arguments)
            except ValidationError as error:
                raise ReportError('INVALID_ARGUMENT', 'arguments',
                                  'Ожидались только run_id в формате UUID и scenario: base, plus10 или compare') from error
            response.run_id, response.scenario = request.run_id, request.scenario
            report, rates = self.registry.read(request.run_id)
            project(report, rates, request, response)
        except ReportError as error:
            response.error = error.payload
        except (KeyError, TypeError, ValueError, AttributeError):
            response.error = Error(code='REPORT_INVALID', field='report', message='Неизвестная или повреждённая структура отчёта')
            response.tool_status = 'ERROR'
            response.numerical_validation = 'NOT_ASSESSED'
            response.comparison = None
        payload = response.model_dump(mode='json')
        # Only normalized permitted arguments are logged. Extra values (tokens,
        # commands or paths in a malformed request) are never copied into traces.
        trace = {'schema_version': 1, 'trace_id': response.trace_id, 'tool': 'read_results',
                 'started_utc': started, 'finished_utc': utc_now(),
                 'arguments': {'run_id': response.run_id, 'scenario': response.scenario}, 'result': payload}
        try:
            write_json(self.registry.directory / 'traces' / (response.trace_id + '.json'), trace)
        except OSError:
            response.tool_status = 'ERROR'
            response.error = Error(code='TRACE_WRITE_FAILED', field='trace_id', message='Не удалось записать журнал вызова')
            payload = response.model_dump(mode='json')
        return payload
