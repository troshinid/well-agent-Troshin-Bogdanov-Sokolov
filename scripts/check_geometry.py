"""Проверка локальных pipe.json и profile.csv; PIPESIM не запускается."""

import argparse
import csv
import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path


def number(value, field):
    if value is None or isinstance(value, bool):
        raise ValueError(f'{field}: нужно число')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'{field}: неверное число {value!r}') from None
    if not result.is_finite():
        raise ValueError(f'{field}: число должно быть конечным')
    return result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_geometry(pipe_path):
    pipe = json.loads(pipe_path.read_text(encoding='utf-8-sig'))
    if not isinstance(pipe, dict):
        raise ValueError('pipe.json: ожидается объект JSON')
    fields = ('outer_diameter_mm', 'wall_thickness_mm', 'inner_diameter_mm',
              'roughness_mm', 'reference_length_m')
    values = {key: number(pipe.get(key), key) for key in fields}
    for key, value in values.items():
        if value < 0 or (key != 'roughness_mm' and value == 0):
            raise ValueError(f'{key}: значение вне допустимого диапазона')
    inner = values['outer_diameter_mm'] - 2 * values['wall_thickness_mm']
    if inner <= 0 or inner != values['inner_diameter_mm']:
        raise ValueError('Диаметры: inner_diameter_mm должен равняться OD − 2 × стенка и быть больше нуля')
    if values['roughness_mm'] >= inner:
        raise ValueError('Шероховатость должна быть меньше внутреннего диаметра')
    for key in ('pipe_id', 'from_node', 'to_node'):
        if not isinstance(pipe.get(key), str) or not pipe[key].strip():
            raise ValueError(f'{key}: нужен непустой идентификатор')
    if pipe['from_node'] == pipe['to_node']:
        raise ValueError('Начало и конец линии должны различаться')
    if not isinstance(pipe.get('profile'), str) or not pipe['profile'].strip():
        raise ValueError('profile: укажите имя CSV, например profile.csv')
    profile_path = (pipe_path.parent / pipe['profile']).resolve()
    with profile_path.open(encoding='utf-8-sig', newline='') as file:
        reader = csv.DictReader(file)
        required = {'point_id', 'station', 'horizontal_distance_m', 'axis_distance_m',
                    'pipe_bottom_m', 'pipe_axis_m', 'source_pdf_page', 'review_status'}
        if not required <= set(reader.fieldnames or []):
            raise ValueError('В profile.csv отсутствуют обязательные колонки')
        rows = list(reader)
    if len(rows) < 2:
        raise ValueError('В профиле должно быть не меньше двух точек')

    ids = set()
    coordinates = []
    reviews = {}
    checked_reviews = {'TEAM_REVIEWED', 'SOURCE_CHECKED', 'SOURCE_CHECKED_WITH_DECISION'}
    allowed_reviews = checked_reviews | {'USER_CHECKED_ELEVATION', 'TRANSCRIBED_REVIEW_PENDING'}
    for line, row in enumerate(rows, start=2):
        prefix = f'CSV, строка {line}'
        if not row['point_id'] or row['point_id'] in ids:
            raise ValueError(f'{prefix}: пустой или повторный point_id')
        ids.add(row['point_id'])
        if not row['station'].strip():
            raise ValueError(f'{prefix}: не указан пикет')
        review = row['review_status']
        if review not in allowed_reviews:
            raise ValueError(f'{prefix}: неизвестный review_status')
        reviews[review] = reviews.get(review, 0) + 1
        try:
            page = int(row['source_pdf_page'])
        except ValueError:
            raise ValueError(f'{prefix}: нужен целый номер страницы PDF') from None
        if page <= 0:
            raise ValueError(f'{prefix}: номер страницы должен быть положительным')
        x = number(row['horizontal_distance_m'], prefix + ', расстояние')
        bottom = number(row['pipe_bottom_m'], prefix + ', низ трубы')
        axis = number(row['pipe_axis_m'], prefix + ', ось трубы')
        distance_axis = number(row['axis_distance_m'], prefix + ', длина по оси')
        if axis != bottom + values['outer_diameter_mm'] / 2000:
            raise ValueError(f'{prefix}: неверный пересчёт высоты низа в высоту оси')
        if coordinates and x <= coordinates[-1][0]:
            raise ValueError(f'{prefix}: горизонтальная координата должна возрастать')
        coordinates.append((x, axis, bottom, distance_axis))
    if coordinates[0][0] != 0 or coordinates[0][3] != 0:
        raise ValueError('Координаты первой точки должны начинаться с нуля')

    length_axis = 0.0
    for i in range(1, len(coordinates)):
        a, b = coordinates[i - 1], coordinates[i]
        dx = float(b[0] - a[0])
        dz = float(b[1] - a[1])
        segment = math.hypot(dx, dz)
        length_axis += segment
        if not math.isfinite(length_axis):
            raise ValueError('Длина по оси вышла за числовой диапазон')
        # Это точность проверки вычисления по CSV, не инженерный допуск.
        if not math.isclose(float(b[3]), length_axis, rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"{rows[i]['point_id']}: неверная накопленная длина по оси")
    horizontal = coordinates[-1][0]
    return {
        'structural_check': 'PASS',
        'points': len(rows),
        'segments': len(rows) - 1,
        'horizontal_length_m': str(horizontal),
        'axis_chord_length_m': length_axis,
        'reference_length_m': str(values['reference_length_m']),
        'horizontal_minus_reference_m': str(horizontal - values['reference_length_m']),
        'elevation_change_m': str(coordinates[-1][1] - coordinates[0][1]),
        'min_pipe_bottom_m': str(min(point[2] for point in coordinates)),
        'max_pipe_bottom_m': str(max(point[2] for point in coordinates)),
        'reviews': reviews,
        'review_complete': sum(reviews.get(status, 0) for status in checked_reviews) == len(rows),
        'review_note': 'Завершение сверки определяется по записанным статусам; скрипт сам PDF не читает',
        'note': 'PASS проверяет структуру и арифметику, а не точность переноса с чертежа или готовность PIPESIM',
        'pipe_sha256': digest(pipe_path),
        'profile_sha256': digest(profile_path),
    }


def main():
    parser = argparse.ArgumentParser(description='Проверка геометрии выбранной трубы')
    parser.add_argument('pipe', type=Path, help='Путь к pipe.json')
    args = parser.parse_args()
    path = args.pipe.resolve()
    try:
        report = check_geometry(path)
        output = path.parent / 'geometry_check.json'
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError, csv.Error) as error:
        parser.error(str(error))
    print(f"Точек: {report['points']}; сегментов: {report['segments']}")
    print(f"Горизонтальная длина: {report['horizontal_length_m']} м")
    print(f"Длина по оси ломаной: {report['axis_chord_length_m']:.4f} м")
    print(f"Перепад высот: {report['elevation_change_m']} м")
    print('Структура и арифметика: PASS')
    print('По статусам строк сверка с источником завершена' if report['review_complete'] else 'Сверка всех строк с чертежом ещё не завершена')
    print(f'Отчёт: {output}')


if __name__ == '__main__':
    main()
