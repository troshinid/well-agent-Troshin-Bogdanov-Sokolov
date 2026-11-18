"""Проверка исходных свойств флюида; настройка PIPESIM выполняется отдельно."""

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


def obj(value, field):
    if not isinstance(value, dict):
        raise ValueError(f'{field}: ожидается объект JSON')
    return value


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field}: нужна непустая строка')
    return value


def number(value, field, *, positive=False):
    if value is None or isinstance(value, bool):
        raise ValueError(f'{field}: нужно число')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'{field}: неверное число {value!r}') from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f'{field}: нужно конечное' + (' положительное' if positive else '') + ' число')
    return result


def check_fluid(path, source_override=None):
    data = obj(json.loads(path.read_text(encoding='utf-8-sig')), 'fluid.json')
    fluid_id = text(data.get('fluid_id'), 'fluid_id')
    if data.get('model') != 'BlackOil':
        raise ValueError('Этот скрипт проверяет конфигурацию BlackOil')
    if data.get('basis') != 'educational_analogue':
        raise ValueError('basis: для текущего флюида требуется educational_analogue')
    if not isinstance(data.get('calculation_ready'), bool):
        raise ValueError('calculation_ready: нужно true или false')

    reference = obj(data.get('reference_conditions'), 'reference_conditions')
    reference_t = number(reference.get('temperature_c'), 'reference temperature')
    if reference_t <= Decimal('-273.15'):
        raise ValueError('Температура должна быть выше абсолютного нуля')
    reference_p = number(reference.get('pressure_pa'), 'reference pressure', positive=True)
    if reference.get('pressure_scale') != 'absolute':
        raise ValueError('Для reference_conditions нужна абсолютная шкала давления')
    if reference.get('origin') != 'scenario_assumption':
        raise ValueError('Условия учёта в текущем сценарии должны быть отмечены как scenario_assumption')

    oil = obj(data.get('oil'), 'oil')
    text(oil.get('source_formation'), 'oil.source_formation')
    if oil.get('sample_type') != 'separated_oil':
        raise ValueError('Опорные вязкости относятся к separated_oil')
    properties = oil.get('properties')
    if not isinstance(properties, list) or len(properties) < 2:
        raise ValueError('Для выбранного описания нужны минимум две температурные точки нефти')
    derived = []
    last_t = None
    for index, raw in enumerate(properties):
        row = obj(raw, f'oil.properties[{index}]')
        t = number(row.get('temperature_c'), f'Точка {index}: температура')
        if t <= Decimal('-273.15') or (last_t is not None and t <= last_t):
            raise ValueError('Температуры должны быть выше абсолютного нуля и строго возрастать')
        density = number(row.get('density_kg_m3'), f'Точка {index}: плотность', positive=True)
        viscosity = number(row.get('kinematic_viscosity_mm2_s'), f'Точка {index}: вязкость', positive=True)
        # (кг/м³) × (мм²/с) / 1000 = мПа·с.
        derived.append({'temperature_c': t, 'density_kg_m3': density,
                        'kinematic_viscosity_mm2_s': viscosity,
                        'dynamic_viscosity_mpa_s': density * viscosity / 1000})
        last_t = t
    at_reference = next((row for row in derived if row['temperature_c'] == reference_t), None)
    if at_reference is None:
        raise ValueError('Нет плотности нефти при выбранной температуре учёта; автоматическая экстраполяция не выполняется')

    gas = obj(data.get('gas'), 'gas')
    molar_mass = number(gas.get('molar_mass_g_mol'), 'gas.molar_mass_g_mol', positive=True)
    assumed_air_molar_mass = Decimal('28.96546')
    water = obj(data.get('water'), 'water')
    if water.get('type') != 'pure_water':
        raise ValueError('Для текущего сценария задана pure_water')
    if number(water.get('salinity_mass_fraction'), 'water.salinity_mass_fraction') != 0:
        raise ValueError('Для pure_water массовая доля солей должна быть равна нулю')
    if water.get('origin') != 'scenario_assumption':
        raise ValueError('Пресная вода должна быть отмечена как scenario_assumption')

    sources = obj(data.get('sources'), 'sources')
    relative_source = text(sources.get('file'), 'sources.file')
    expected_hash = text(sources.get('sha256'), 'sources.sha256')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', expected_hash):
        raise ValueError('sources.sha256: неверный формат SHA-256')
    source = Path(source_override) if source_override is not None else path.parent.parent / relative_source
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_hash != expected_hash.lower():
        raise ValueError('SHA-256 исходного PDF не совпадает: используется другой файл')

    pending = []
    if not data.get('pvt_correlations'):
        pending.append('Корреляции свойств BlackOil')
    if not data.get('mixture_viscosity_model'):
        pending.append('Правило расчёта вязкости водонефтяной смеси')
    if data['calculation_ready'] and pending:
        raise ValueError('calculation_ready=true при незаполненных настройках флюида')

    return {
        'input_check': 'PASS', 'fluid_id': fluid_id, 'basis': data['basis'],
        'reference_conditions': {'temperature_c': reference_t, 'pressure_pa': reference_p, 'pressure_scale': 'absolute'},
        'oil_properties': derived,
        'oil_density_at_reference_kg_m3': at_reference['density_kg_m3'],
        'gas_molar_mass_g_mol': molar_mass,
        'assumed_air_molar_mass_g_mol': assumed_air_molar_mass,
        'gas_relative_density_to_air_ideal': molar_mass / assumed_air_molar_mass,
        'source_sha256': source_hash,
        'fluid_config_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'pending_settings': pending,
        'pipesim_integration_verified': False,
        'note': 'PASS проверяет входные числа и источник. Вязкости относятся к сепарированной нефти, не к смеси; настройки и применимость корреляций проверяются в PIPESIM отдельно.',
    }


def main():
    parser = argparse.ArgumentParser(description='Проверка свойств флюида-аналога')
    parser.add_argument('fluid', type=Path, help='Путь к fluid.json в well-agent-data/fluid')
    args = parser.parse_args()
    path = args.fluid.resolve()
    try:
        report = check_fluid(path)
        output = path.parent / 'fluid_check.json'
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print('Данные и SHA-256 исходника: PASS')
    for row in report['oil_properties']:
        print(f"Нефть при {row['temperature_c']} °C: вязкость {row['dynamic_viscosity_mpa_s']:.6f} мПа·с")
    print(f"Относительная плотность газа по воздуху, идеальное приближение: {report['gas_relative_density_to_air_ideal']:.6f}")
    for setting in report['pending_settings']:
        print(f'Не выбрано: {setting}')
    print('Интеграция флюида с PIPESIM ещё не проверена')
    print(f'Отчёт: {output}')


if __name__ == '__main__':
    main()
