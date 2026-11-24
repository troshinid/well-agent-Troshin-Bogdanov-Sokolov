"""Render numbers only from validated tool payloads; never from LLM text."""

REASONS = {
    'OUT_OF_SCOPE': 'Запрос выходит за границы выбранного участка и стационарных сценариев.',
    'AMBIGUOUS': 'Уточни объект, операцию и единицы. Зависимый расчёт не запускался.',
    'MODEL_CHANGE': 'Этот вариант агента не изменяет параметры модели. Нужен отдельный план и подтверждение изменений.',
    'UNTRUSTED_INSTRUCTION': 'Запрос содержит недопустимую инструкцию. Доступа к командам, файлам и ключам у LLM нет.',
}


def quantity(value):
    return 'нет данных' if value is None else format(value, '.12g')


def build_answer(evidence, action):
    if not evidence:
        return 'Расчётные данные не получены.'
    payload = evidence[-1]['payload']
    lines = ['Запрос на новый расчёт согласованной пары base/plus10 через MCP.' if action == 'run_steady_state'
             else 'Чтение сохранённых результатов через MCP; новый расчёт не запускался.']
    seen_errors = set()
    for event in evidence:
        item = event['payload']
        if item.get('reused'):
            lines.append('Повтор request_id: использован уже зарегистрированный запуск.')
        if item['error']:
            key = (item['error']['code'], item['error']['message'])
            if key not in seen_errors:
                lines.append('Ошибка ' + key[0] + ': ' + key[1])
                seen_errors.add(key)
        if item.get('job_state') == 'OUTCOME_UNKNOWN':
            lines.append('Исход запуска пока неизвестен. Повтори тот же request_id; новый запрос не создавай.')
    lines.append('run_id: ' + (payload['run_id'] or 'не зарегистрирован'))
    for name, result in payload['results'].items():
        lines.append(name + ': P входа = ' + quantity(result['inlet_pressure']['value'])
                     + ' bara; P выхода = ' + quantity(result['outlet_pressure']['value'])
                     + ' bara; расход жидкости = ' + quantity(result['liquid_rate_source']['value']) + ' sm3/day'
                     + '; источник = ' + result['result_origin'] + '.')
        mass = result.get('mass_balance')
        if mass and mass['source']['value'] is not None and mass['sink']['value'] is not None:
            lines.append(name + ': массовый расход входа = ' + quantity(mass['source']['value'])
                         + ' kg/s; выхода = ' + quantity(mass['sink']['value'])
                         + ' kg/s; невязка (вход − выход) = ' + quantity(mass['signed_residual_kg_s'])
                         + ' kg/s; относительная невязка по модулю = '
                         + ('не определена' if mass['relative_residual_percent'] is None else
                            quantity(mass['relative_residual_percent']) + '%')
                         + '; оценка массового баланса: ' + mass['assessment'] + '.')
            if mass['reason'] == 'NO_APPROVED_TOLERANCE':
                lines.append('Допуск массового баланса не утверждён; невязка приведена без оценки соответствия.')
            elif mass['reason'] == 'ZERO_REFERENCE_REQUIRES_ABSOLUTE_TOLERANCE':
                lines.append('При нулевом входном расходе нужен утверждённый абсолютный допуск массового баланса.')
        else:
            lines.append(name + ': массовые расходы не получены; оценка массового баланса: NOT_ASSESSED.')
            if result.get('mass_balance_error'):
                lines.append(name + ': ошибка экспорта массовых расходов: ' + result['mass_balance_error'])
    if payload['comparison']:
        delta = payload['comparison']['inlet_pressure_plus10_minus_base']['value']
        lines.append('Разница P входа (plus10 − base) = ' + quantity(delta) + ' bar ('
                     + quantity(delta * 100000) + ' Pa). Это сравнение двух стационарных режимов.')
    lines.append('Численные проверки: ' + payload['numerical_validation'] + '.')
    seen = set()
    for event in evidence:
        for warning in event['payload']['warnings']:
            key = (warning['scenario'], warning['stage'], warning['message'])
            if key not in seen:
                lines.append('Предупреждение [' + warning['scenario'] + '/' + warning['stage'] + ']: ' + warning['message'])
                seen.add(key)
    lines.append('Соответствие промысловым измерениям не оценивалось (NOT_ASSESSED).')
    if payload['results']:
        lines.extend([
            'Ограничения данных по участку АГЗУ-6:',
            'В предоставленных материалах нет сопоставимых замеров давления на обоих концах линии '
            'и полного расхода за один устойчивый режим.',
            'Расход задан по скважинам 113 и 115. По скважинам 114 и 116 данных о расходах нет. '
            'Полный фактический приток неизвестен.',
            'Давление на врезке 10 бар абсолютных, температура 20 °C и условия учёта объёмов '
            '(20 °C, 101325 Па абсолютных) приняты для учебных сценариев.',
            'Флюид задан по аналогу Т1, его соответствие скважинам 113 и 115 не подтверждено. '
            'Вода принята пресной; дополнительные местные сопротивления не учтены.',
        ])
    return '\n'.join(lines)
