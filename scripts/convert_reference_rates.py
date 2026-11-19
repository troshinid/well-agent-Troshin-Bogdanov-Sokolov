"""Engineering reference-volume conversion, not a PIPESIM/PVT calculation (3.8+).

Oil and water conserve mass. Total gas component conserves moles with Z=1 at
both reference states. Liquid pressure effects between the two near-atmospheric
states are neglected. No separator/flash calculation is performed here.
"""

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from check_fluid import check_fluid


D = Decimal
PSI_TO_PA = D('6894.757293168')
STOCK_T_C = (D(60) - 32) * 5 / 9
STOCK_P_PA = D('14.7') * PSI_TO_PA
WATER_SOURCE = 'https://www.nist.gov/system/files/documents/2019/05/14/glp-10-purity-water-20190507.pdf'


def number(value):
    try:
        result = D(str(value))
    except InvalidOperation:
        raise ValueError('Требуется число: {!r}'.format(value)) from None
    if not result.is_finite():
        raise ValueError('Требуется конечное число')
    return result


def water_density(t):
    """NIST GLP 10 (2019), Eq. 1: air-free water, kg/m3, 0..40 degC."""
    if not D(0) <= t <= D(40):
        raise ValueError('Формула плотности воды применима от 0 до 40 °C')
    return D('999.974950') * (1 - ((t - D('3.983035')) ** 2 * (t + D('301.797')))
                            / (D('522528.9') * (t + D('69.34881'))))


def convert(checked, liquid, water, gas, stock_oil_density):
    liquid, water, gas, stock_oil_density = map(number, (liquid, water, gas, stock_oil_density))
    if liquid <= 0 or not 0 <= water < liquid or gas < 0 or stock_oil_density <= 0:
        raise ValueError('Нужны: жидкость > 0, 0 ≤ вода < жидкость, газ ≥ 0, плотность > 0')
    ref = checked['reference_conditions']
    t_in, p_in = number(ref['temperature_c']), number(ref['pressure_pa'])
    # This approximation is deliberately limited to near-atmospheric references.
    if not D('95000') <= p_in <= D('105000'):
        raise ValueError('Пересчёт без сжимаемости жидкости ограничен околoатмосферными условиями')
    rho_o_in = number(checked['oil_density_at_reference_kg_m3'])
    rho_w_in, rho_w_out = water_density(t_in), water_density(STOCK_T_C)
    oil = liquid - water
    oil_out = oil * (rho_o_in / stock_oil_density)
    water_out = water * (rho_w_in / rho_w_out)
    gas_out = gas * (p_in / STOCK_P_PA) * ((STOCK_T_C + D('273.15')) / (t_in + D('273.15')))
    liquid_out = oil_out + water_out
    watercut = 100 * water_out / liquid_out
    gor = gas_out / oil_out

    def rates(qo, qw, qg):
        return {'oil_m3_day': qo, 'water_m3_day': qw,
                'liquid_m3_day': qo + qw, 'gas_m3_day': qg}

    return {
        'status': 'REFERENCE_CONVERSION_COMPLETE',
        'method': 'liquid_mass_balance_and_ideal_gas_mole_balance',
        'hydraulic_calculation_run': False,
        'fluid_config_sha256': checked['fluid_config_sha256'],
        'source_pdf_sha256': checked['source_sha256'],
        'input_conditions': ref,
        'stock_tank_conditions': {'temperature_c': STOCK_T_C, 'pressure_pa': STOCK_P_PA,
                                  'pressure_scale': 'absolute',
                                  'basis': 'PIPESIM 2021.1 Help: Stock tank and flowing conditions, 60 F / 14.7 psia',
                                  'note': 'Use the imperial row; the metric row in Help is approximate.'},
        'input_rates': rates(oil, water, gas),
        'input_rates_origin': 'Caller-supplied scenario; for 113+115 see docs/model_scope.md',
        'density_kg_m3': {'oil_input': rho_o_in, 'oil_stock': stock_oil_density,
                          'water_input': rho_w_in, 'water_stock': rho_w_out},
        'stock_oil_density_origin': 'Caller-supplied DOD; current case: 854.91 kg/m3 from T1 table 29, interpolation 10-20 C',
        'water_density_source': {'url': WATER_SOURCE, 'equation': 1,
                                  'medium': 'pure_air_free_water', 'temperature_range_c': [0, 40]},
        'assumptions': ['Gas compressibility Z=1 at both reference states',
                        'Liquid pressure effects near atmospheric pressure neglected',
                        'Separate conservation of oil and water mass; no separator flash',
                        'Pure air-free water density from NIST; not measured produced-water density'],
        'formulas': {'liquids': 'Q_out = Q_in * rho_in / rho_out',
                      'gas': 'Q_out = Q_in * p_in/p_out * T_out_K/T_in_K (Z_out=Z_in=1)',
                      'watercut_percent': '100 * Q_water_out / Q_liquid_out',
                      'GOR': 'Q_gas_out / Q_oil_out'},
        'base_stock_rates': rates(oil_out, water_out, gas_out),
        'plus10_stock_rates': rates(oil_out * D('1.1'), water_out * D('1.1'), gas_out * D('1.1')),
        'manual_fields': {'watercut_percent': watercut, 'gor_sm3_per_sm3': gor,
                          'gor_scf_per_stb': gor * D('0.158987294928') / D('0.028316846592')},
        'ratios_same_for_base_and_plus10': True,
    }


def main():
    parser = argparse.ArgumentParser(description='Пересчёт расходов к опорным условиям PIPESIM')
    parser.add_argument('fluid', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--liquid', required=True, help='Жидкость при входных условиях, м³/сут')
    parser.add_argument('--water', required=True, help='Вода при входных условиях, м³/сут')
    parser.add_argument('--gas', required=True, help='Полный газовый компонент при входных условиях, м³/сут')
    parser.add_argument('--stock-oil-density', required=True, help='Назначенная DOD, кг/м³')
    args = parser.parse_args()
    try:
        output = args.output.resolve()
        if output.suffix.lower() != '.json' or output.exists():
            raise ValueError('Укажите новый выходной файл .json')
        checked = check_fluid(args.fluid.resolve())
        report = convert(checked, args.liquid, args.water, args.gas, args.stock_oil_density)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Decimal values are strings to retain calculation precision.
        with output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    fields = report['manual_fields']
    print('Watercut: {:.5f} %'.format(fields['watercut_percent']))
    print('GOR: {:.5f} sm3/sm3'.format(fields['gor_sm3_per_sm3']))
    print('GOR: {:.5f} SCF/STB'.format(fields['gor_scf_per_stb']))
    print('Жидкость (база): {:.5f} sm3/day'.format(report['base_stock_rates']['liquid_m3_day']))
    print('Отчёт: {}'.format(output))


if __name__ == '__main__':
    main()
