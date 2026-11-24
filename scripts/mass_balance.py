"""Total mass conservation for Source -> Flowline -> Sink (Python 3.8+).

Rates are positive magnitudes along the verified direction, with no intermediate
withdrawals. Negative/reverse flow requires a separate contract, never abs().
"""

import math

SOURCE, SINK = 'agzu6_out', 'tiein_agzu4_agzu1'
EMPTY_CRITERIA = {'absolute_tolerance_kg_s': None, 'relative_tolerance': None,
                  'approval_reference': None}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Mass balance requires finite numeric values')
    return float(value)


def criteria(value=None):
    value = dict(EMPTY_CRITERIA) if value is None else value
    if not isinstance(value, dict) or set(value) != set(EMPTY_CRITERIA):
        raise ValueError('Unknown mass balance criteria fields')
    result = dict(value)
    for key in ('absolute_tolerance_kg_s', 'relative_tolerance'):
        if result[key] is not None:
            result[key] = number(result[key])
            if result[key] < 0:
                raise ValueError('Mass balance tolerances must be nonnegative')
    reference = result['approval_reference']
    if reference is not None and (not isinstance(reference, str) or not reference.strip()):
        raise ValueError('Invalid approval reference')
    if any(result[key] is not None for key in ('absolute_tolerance_kg_s', 'relative_tolerance')) and not reference:
        raise ValueError('A tolerance requires an explicit approval reference')
    return result


def kg_per_second(value, unit):
    key = str(unit).strip().lower().replace(' ', '')
    scales = {'kg/s': 1.0, 'kg/sec': 1.0, 'kg/h': 1 / 3600, 'kg/hr': 1 / 3600,
              'kg/d': 1 / 86400, 'kg/day': 1 / 86400,
              'g/s': 0.001, 'lb/s': 0.45359237, 'lb/sec': 0.45359237,
              'lbm/s': 0.45359237, 'lbm/sec': 0.45359237,
              'lb/h': 0.45359237 / 3600, 'lb/hr': 0.45359237 / 3600,
              'lbm/h': 0.45359237 / 3600, 'lbm/hr': 0.45359237 / 3600}
    if key not in scales:
        raise ValueError('Unsupported mass flow unit: ' + str(unit))
    return number(number(value) * scales[key])


def unavailable(criterion=None, reason='NOT_EXPORTED', variable='MassFlowrateFluid'):
    if variable not in ('MassFlowrateFluid', 'MassFlowrate'):
        raise ValueError('Unsupported mass flow variable')
    return {'source': {'object_id': SOURCE, 'value': None, 'unit': 'kg/s'},
            'sink': {'object_id': SINK, 'value': None, 'unit': 'kg/s'},
            'source_variable': variable,
            'flow_convention': 'positive_in_network_direction',
            'signed_residual_kg_s': None, 'absolute_residual_kg_s': None,
            'relative_residual_percent': None, 'allowed_residual_kg_s': None,
            'criteria': criteria(criterion), 'assessment': 'NOT_ASSESSED', 'reason': reason}


def calculate(source, sink, criterion=None, variable='MassFlowrateFluid'):
    source, sink = number(source), number(sink)
    if source < 0 or sink < 0:
        raise ValueError('Reverse/signed mass flow is not supported by this contract')
    result = unavailable(criterion, variable=variable)
    signed = number(source - sink)
    absolute = abs(signed)
    relative = number(100 * (absolute / source)) if source != 0 else None
    result.update(source={'object_id': SOURCE, 'value': source, 'unit': 'kg/s'},
                  sink={'object_id': SINK, 'value': sink, 'unit': 'kg/s'},
                  signed_residual_kg_s=signed, absolute_residual_kg_s=absolute,
                  relative_residual_percent=relative, reason='NO_APPROVED_TOLERANCE')
    settings = result['criteria']
    absolute_limit, relative_limit = settings['absolute_tolerance_kg_s'], settings['relative_tolerance']
    if absolute_limit is None and relative_limit is None:
        return result
    if source == 0 and absolute_limit is None:
        result['reason'] = 'ZERO_REFERENCE_REQUIRES_ABSOLUTE_TOLERANCE'
        return result
    allowed = max(absolute_limit or 0.0, number(relative_limit * source) if relative_limit is not None else 0.0)
    passed = absolute <= allowed
    result.update(allowed_residual_kg_s=allowed, assessment='OK' if passed else 'OUT_OF_TOLERANCE',
                  reason='WITHIN_TOLERANCE' if passed else 'OUTSIDE_TOLERANCE')
    return result


def extract(payload, criterion=None):
    # Actual Toolkit 2021.1 response: total mass is a profile variable, not a
    # node variable. Read the source branch inlet and the sink branch outlet.
    # Never use both ends of the same pipe as a substitute for the two network
    # boundaries: that can conceal the discrepancy at the connecting node.
    unit = payload['profile_units']['MassFlowrate']
    endpoints = []
    for name, index in ((SOURCE, 0), (SINK, -1)):
        branch = payload['profile'][name]
        values = branch['MassFlowrate']
        if not isinstance(values, list) or not values:
            raise ValueError('Missing mass profile at boundary branch ' + name)
        if any(not isinstance(branch.get(key), list) or len(branch[key]) != len(values)
               for key in ('Pressure', 'TotalDistance')):
            raise ValueError('Mass profile columns are not aligned at ' + name)
        for value in values:
            number(value)
        endpoints.append(kg_per_second(values[index], unit))
    return calculate(endpoints[0], endpoints[1], criterion, variable='MassFlowrate')
