"""Run only a registered prepared model in a separate activated PTK process.

No process arguments, paths, temperatures or model edits are accepted from MCP.
On timeout the process is NOT killed or submitted again. The exit receipt lets
a later call reconcile the same request after a client/server restart.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .results import ReadResults, Registry, ReportError, checksum, parse_json, project, read_bytes, utc_now, write_json
from .schemas import Error, ReadRequest, RunRequest, RunResponse


REPOSITORY = Path(__file__).resolve().parents[2]
CODE_FILES = ['scripts/pipesim_worker.cmd', 'scripts/run_network_model.py', 'scripts/build_network_model.py',
              'scripts/build_fluid_model.py', 'scripts/check_fluid.py', 'scripts/check_geometry.py',
              'scripts/create_fluid_draft.py', 'scripts/convert_reference_rates.py']


def license_failure(folder):
    path = folder / 'worker.log'
    text = path.read_bytes()[-1000000:].decode('utf-8', errors='replace').lower() if path.is_file() else ''
    return any(term in text for term in ['license checkout failed', 'no valid license',
        'no available licenses', 'failed to check out', 'license could not', 'license is not available'])


def file_hash(path, field):
    return checksum(read_bytes(path, field))


def safe_command_path(path):
    text = str(Path(path).resolve())
    # activate.bat uses CALL (a second cmd expansion); keep its paths literal.
    if any(c in text for c in '\r\n"%&|<>^!'):
        raise ReportError('INVALID_ARGUMENT', 'local_path', 'Путь для запуска cmd содержит неподдерживаемые символы')
    return text


def profile_config(registry, seed_run_id, prepared_model, toolkit_root, output_root, wait_seconds=600):
    ReadRequest(run_id=seed_run_id, scenario='compare')
    reg = Registry(registry)
    if (reg.directory / 'runner.lock').exists():
        raise ReportError('BUSY', 'runner', 'Сначала проверь результат незавершённого request_id')
    report, _ = reg.read(seed_run_id)
    if report['status'] not in ['NUMERICAL_CHECKS_PASSED', 'NUMERICAL_CHECKS_PASSED_WITH_WARNINGS']:
        raise ReportError('INPUT_MISMATCH', 'seed_run_id', 'Нужен полностью проверенный исходный расчёт')
    verification = ReadResults(registry).call({'run_id': seed_run_id, 'scenario': 'compare'})
    if verification['tool_status'] != 'SUCCESS' or verification['numerical_validation'] != 'OK':
        raise ReportError('INPUT_MISMATCH', 'seed_run_id', 'Исходный расчёт не прошёл проверку read_results')
    prepared = safe_command_path(prepared_model)
    if file_hash(prepared, 'prepared_model') != report.get('model_sha256'):
        raise ReportError('INPUT_MISMATCH', 'prepared_model', 'Подготовленная модель не соответствует отчёту')
    toolkit = Path(safe_command_path(toolkit_root))
    for relative in ['python.exe', 'Scripts/activate.bat']:
        if not (toolkit / relative).is_file():
            raise ReportError('TOOLKIT_UNAVAILABLE', 'toolkit_root', 'Не найден файл Toolkit: ' + relative)
    output = Path(safe_command_path(output_root))
    safe_command_path(REPOSITORY)
    if type(wait_seconds) is not int or not 1 <= wait_seconds <= 7200:
        raise ReportError('INVALID_ARGUMENT', 'wait_seconds', 'Допустимое ожидание: от 1 до 7200 секунд')
    config = {'schema_version': 1, 'seed_run_id': seed_run_id, 'prepared_model': prepared,
              'prepared_sha256': report['model_sha256'], 'toolkit_root': str(toolkit),
              'output_root': str(output), 'wait_seconds': wait_seconds,
              'code_hashes': {name: file_hash(REPOSITORY / name, name) for name in CODE_FILES}}
    write_json(reg.directory / 'runner.json', config)
    return config


def load_profile(registry, seed_run_id):
    path = registry.directory / 'runner.json'
    if not path.is_file():
        raise ReportError('RUNNER_NOT_CONFIGURED', 'runner', 'Выполни локальную команду configure-runner')
    profile = parse_json(read_bytes(path, 'runner'), 'runner')
    if profile.get('schema_version') != 1 or profile.get('seed_run_id') != seed_run_id:
        raise ReportError('INPUT_MISMATCH', 'seed_run_id', 'В профиле разрешён другой исходный расчёт')
    try:
        for name in CODE_FILES:
            if file_hash(REPOSITORY / name, name) != profile['code_hashes'][name]:
                raise ReportError('INPUT_MISMATCH', 'runner.code', 'Код изменён после настройки профиля: ' + name)
        safe_command_path(profile['toolkit_root']);safe_command_path(profile['output_root'])
        safe_command_path(profile['prepared_model'])
        if (type(profile['wait_seconds']) is not int or not 1 <= profile['wait_seconds'] <= 7200 or
                file_hash(profile['prepared_model'], 'prepared_model') != profile['prepared_sha256']):
            raise ReportError('INPUT_MISMATCH', 'runner', 'Изменена подготовленная модель или настройки ожидания')
    except (KeyError, TypeError) as error:
        raise ReportError('REPORT_INVALID', 'runner', 'Повреждён локальный профиль запуска') from error
    return profile


class WindowsExecutor:
    """Only a fixed relative batch filename is passed to cmd.exe."""
    def start(self, profile, files, folder):
        if sys.platform != 'win32':
            raise ReportError('TOOLKIT_UNAVAILABLE', 'platform', 'PIPESIM 2021.1 запускается на Windows')
        for rel in ['python.exe', 'Scripts/activate.bat']:
            if not (Path(profile['toolkit_root']) / rel).is_file():
                raise ReportError('TOOLKIT_UNAVAILABLE', 'toolkit_root', 'Не найден установленный Toolkit')
        env = os.environ.copy()
        env.update({'PYTHONIOENCODING': 'utf-8',
                    'WELL_AGENT_PTK_ROOT': profile['toolkit_root'],
                    'WELL_AGENT_RUN_SCRIPT': safe_command_path(REPOSITORY / 'scripts/run_network_model.py'),
                    'WELL_AGENT_SOURCE_MODEL': safe_command_path(files['source_model']['path']),
                    'WELL_AGENT_RATES': safe_command_path(files['rates']['path']),
                    'WELL_AGENT_PREPARED_MODEL': profile['prepared_model'],
                    'WELL_AGENT_SEED_REPORT': safe_command_path(files['report']['path']),
                    'WELL_AGENT_OUTPUT_DIR': safe_command_path(folder / 'calculation'),
                    'WELL_AGENT_RECEIPT': safe_command_path(folder / 'exit_code.txt')})
        # Relative literal command: no interpolated paths or MCP-controlled shell text.
        command = [str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe'),
                   '/d', '/v:off', '/c', 'scripts\\pipesim_worker.cmd']
        with (folder / 'worker.log').open('xb') as log:
            return subprocess.Popen(command, cwd=REPOSITORY, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT)


class RunSteadyState:
    def __init__(self, registry, executor=None):
        self.registry = Registry(registry)
        self.executor = executor or WindowsExecutor()
        self.processes = {}
        self.mutex = threading.Lock()

    def _unlock(self, request_id):
        path = self.registry.directory / 'runner.lock'
        if path.is_file():
            owner = parse_json(read_bytes(path, 'runner.lock'), 'runner.lock')
            if owner.get('request_id') == request_id:
                path.unlink()

    def _begin(self, request, profile, fingerprint):
        lock = self.registry.directory / 'runner.lock'
        try:
            with lock.open('x', encoding='utf-8') as stream:
                json.dump({'request_id': request.request_id, 'created_utc': utc_now()}, stream)
        except FileExistsError as error:
            owner = parse_json(read_bytes(lock, 'runner.lock'), 'runner.lock').get('request_id')
            raise ReportError('BUSY', 'request_id', 'Есть незавершённый request_id: ' + str(owner)) from error
        folder = Path(profile['output_root']) / request.request_id
        job = None
        path = self.registry.directory / 'jobs' / (request.request_id + '.json')
        try:
            folder.mkdir(parents=True, exist_ok=False)
            job = {'schema_version': 1, 'request': request.model_dump(), 'fingerprint': fingerprint, 'profile': profile,
                   'folder': str(folder), 'state': 'STARTING', 'created_utc': utc_now()}
            write_json(path, job)
            entry = parse_json(read_bytes(self.registry.directory / (request.seed_run_id + '.json'), 'registry'), 'registry')
            process = self.executor.start(profile, entry['files'], folder)
            self.processes[request.request_id] = process
            job.update(state='RUNNING', pid=process.pid)
            write_json(path, job)
            return job
        except Exception as error:
            # Only release when process creation never succeeded. If a child may
            # already be running, keep the lock and the job for reconciliation.
            if request.request_id not in self.processes:
                try:
                    if job is not None:
                        job.update(state='NOT_STARTED', launch_error=(error.payload.model_dump() if isinstance(error, ReportError)
                            else {'code': 'WORKER_FAILED', 'field': 'runner', 'message': 'Не удалось создать расчётный процесс'}))
                        write_json(path, job)
                finally:
                    self._unlock(request.request_id)
            raise

    def _finish(self, request, profile, job, response):
        # Another retry may have finalized this same job while the original
        # caller was waiting. Retain its sealed report ID instead of overwriting
        # the latest job record with the caller's older in-memory snapshot.
        current = parse_json(read_bytes(self.registry.directory / 'jobs' / (request.request_id + '.json'), 'job'), 'job')
        if current.get('fingerprint') != job.get('fingerprint'):
            raise ReportError('INPUT_MISMATCH', 'job', 'Изменена запись выполняемой задачи')
        job = current
        folder = Path(job['folder'])
        receipt = folder / 'exit_code.txt'
        process = self.processes.get(request.request_id)
        exit_code = None
        if receipt.is_file():
            try:
                exit_code = int(receipt.read_text(encoding='ascii').strip())
            except (ValueError, OSError) as error:
                raise ReportError('REPORT_INVALID', 'worker.receipt', 'Некорректный код завершения процесса') from error
        elif process is not None and process.poll() is not None:
            exit_code = process.returncode
        elif job.get('state') in ['FINISHED', 'FAILED'] and type(job.get('exit_code')) is int:
            exit_code = job['exit_code']
        if exit_code is None:
            job['state'] = 'OUTCOME_UNKNOWN'
            write_json(self.registry.directory / 'jobs' / (request.request_id + '.json'), job)
            response.job_state = 'OUTCOME_UNKNOWN'
            response.execution_status = 'UNKNOWN'
            raise ReportError('TIMEOUT', 'request_id', 'Ожидание истекло. Процесс может продолжаться; повтори тот же request_id')
        response.worker_exit_code = exit_code
        job.update(exit_code=exit_code, state='FINISHED' if exit_code == 0 else 'FAILED')
        write_json(self.registry.directory / 'jobs' / (request.request_id + '.json'), job)
        self.processes.pop(request.request_id, None)
        self._unlock(request.request_id)
        response.job_state = job['state']
        response.execution_status = 'FAILED'
        # Check the prepared input again, even when the worker reported an error.
        if file_hash(profile['prepared_model'], 'prepared_model') != profile['prepared_sha256']:
            raise ReportError('INPUT_MISMATCH', 'prepared_model', 'Во время расчёта изменена исходная подготовленная модель')
        entry = parse_json(read_bytes(self.registry.directory / (request.seed_run_id + '.json'), 'registry'), 'registry')
        report_path = folder / 'calculation/report.json'
        if report_path.is_file() or job.get('result_run_id') is not None:
            new_id = job.get('result_run_id')
            if new_id is None:
                candidate = parse_json(read_bytes(report_path, 'report'), 'report')
                if (candidate.get('input_configuration_mode') != 'READ_ONLY_PREPARED_MODEL' or
                        candidate.get('prepared_model_unchanged') is not True or
                        candidate.get('prepared_model_sha256') != profile['prepared_sha256'] or
                        candidate.get('seed_report_sha256') != entry['files']['report']['sha256'] or
                        candidate.get('script_sha256') != profile['code_hashes']['scripts/run_network_model.py']):
                    raise ReportError('INPUT_MISMATCH', 'worker.inputs', 'Отчёт не подтверждает запуск согласованной подготовленной модели')
                for name in CODE_FILES:
                    if file_hash(REPOSITORY / name, name) != profile['code_hashes'][name]:
                        raise ReportError('INPUT_MISMATCH', 'runner.code', 'Во время расчёта изменён код: ' + name)
                new_id = self.registry.register(report_path, entry['files']['source_model']['path'], entry['files']['rates']['path'])
                job['result_run_id'] = new_id
                write_json(self.registry.directory / 'jobs' / (request.request_id + '.json'), job)
            response.run_id = new_id
            new_report, rates = self.registry.read(new_id)
            try:
                project(new_report, rates, ReadRequest(run_id=new_id, scenario='compare'), response)
            except ReportError:
                if exit_code != 0 and license_failure(folder):
                    raise ReportError('LICENSE_UNAVAILABLE', 'worker', 'Toolkit сообщил об отказе лицензии; доступные результаты сохранены')
                raise
            if exit_code != 0:
                response.tool_status = 'ERROR'
                raise ReportError('WORKER_FAILED', 'worker_exit_code', 'Процесс Toolkit завершился с ошибкой; доступные результаты сохранены')
        else:
            code = 'RESULT_INCOMPLETE' if exit_code == 0 else 'LICENSE_UNAVAILABLE' if license_failure(folder) else 'WORKER_FAILED'
            if exit_code == 0:
                response.execution_status = 'UNKNOWN'
            raise ReportError(code, 'worker_exit_code', 'Процесс не создал отчёт. Проверь worker.log для данного request_id')

    def call(self, arguments):
        started = utc_now()
        response = RunResponse(trace_id=str(uuid4()), run_id=None, scenario='compare', tool_status='ERROR',
            execution_status='NOT_RUN', numerical_validation='NOT_ASSESSED', results={}, comparison=None,
            warnings=[], report_ref=None, stock_tank_conditions=None, numerical_limits={}, error=None,
            seed_run_id=None, request_id=None, job_state='NOT_STARTED', worker_exit_code=None, reused=False)
        try:
            try:
                request = RunRequest.model_validate(arguments)
            except ValidationError as error:
                raise ReportError('INVALID_ARGUMENT', 'arguments', 'Ожидались seed_run_id, request_id (UUID) и scenario_set: base_plus10') from error
            response.seed_run_id, response.request_id = request.seed_run_id, request.request_id
            self.registry.read(request.seed_run_id)
            profile = load_profile(self.registry, request.seed_run_id)
            fingerprint = checksum(json.dumps({'request': request.model_dump(), 'profile': profile}, sort_keys=True).encode())
            job_path = self.registry.directory / 'jobs' / (request.request_id + '.json')
            with self.mutex:
                if job_path.is_file():
                    job = parse_json(read_bytes(job_path, 'job'), 'job')
                    if (job.get('fingerprint') != fingerprint or job.get('request') != request.model_dump()
                            or job.get('profile') != profile):
                        raise ReportError('REQUEST_CONFLICT', 'request_id', 'Этот request_id уже использовался с другими параметрами')
                    if Path(job['folder']) != Path(profile['output_root']) / request.request_id:
                        raise ReportError('INPUT_MISMATCH', 'job', 'Изменена папка зарегистрированной задачи')
                    response.reused = True
                else:
                    job = self._begin(request, profile, fingerprint)
            if job.get('state') == 'NOT_STARTED' and job.get('launch_error'):
                response.error = Error.model_validate(job['launch_error'])
                raise ReportError(response.error.code, response.error.field, response.error.message)
            if not response.reused:
                try:
                    self.processes[request.request_id].wait(timeout=profile['wait_seconds'])
                except subprocess.TimeoutExpired:
                    pass
            with self.mutex:
                self._finish(request, profile, job, response)
        except ReportError as error:
            response.tool_status = 'ERROR';response.error = error.payload
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            response.tool_status = 'ERROR'
            response.error = Error(code='WORKER_FAILED', field='runner', message='Не удалось выполнить запуск; проверь локальный профиль и журнал')
            if response.request_id in self.processes:
                response.execution_status = 'UNKNOWN';response.job_state = 'OUTCOME_UNKNOWN'
        payload = response.model_dump(mode='json')
        trace = {'schema_version': 1, 'trace_id': response.trace_id, 'tool': 'run_steady_state',
                 'started_utc': started, 'finished_utc': utc_now(),
                 'arguments': {'seed_run_id': response.seed_run_id, 'request_id': response.request_id,
                               'scenario_set': 'base_plus10' if response.seed_run_id else None}, 'result': payload}
        try:
            write_json(self.registry.directory / 'traces' / (response.trace_id + '.json'), trace)
        except OSError:
            response.tool_status = 'ERROR'
            response.error = Error(code='TRACE_WRITE_FAILED', field='trace_id', message='Не удалось записать журнал вызова')
            payload = response.model_dump(mode='json')
        return payload
