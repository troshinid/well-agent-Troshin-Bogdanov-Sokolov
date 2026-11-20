"""Version 1 payloads. The MCP protocol version is negotiated by the SDK."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


UUID_PATTERN = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
Execution = Literal['SUCCESS', 'PARTIAL', 'FAILED', 'NOT_RUN', 'UNKNOWN']
Validation = Literal['OK', 'OUT_OF_TOLERANCE', 'NOT_ASSESSED']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ReadRequest(StrictModel):
    run_id: str = Field(pattern=UUID_PATTERN, description='UUID зарегистрированного расчёта')
    scenario: Literal['base', 'plus10', 'compare']


class Quantity(StrictModel):
    object_id: str
    value: FiniteFloat | None


class Pressure(Quantity):
    value: FiniteFloat | None = Field(gt=0)
    unit: Literal['bar'] = 'bar'
    reference: Literal['absolute'] = 'absolute'


class Temperature(Quantity):
    value: FiniteFloat | None = Field(gt=-273.15)
    unit: Literal['degC'] = 'degC'


class LiquidRate(Quantity):
    value: FiniteFloat | None = Field(ge=0)
    unit: Literal['sm3/day'] = 'sm3/day'
    conditions_ref: Literal['scenario_rates.stock_tank_conditions'] = 'scenario_rates.stock_tank_conditions'


class Profile(StrictModel):
    points: int | None = Field(ge=1)
    temperature_min: Temperature
    temperature_max: Temperature


class ScenarioResult(StrictModel):
    result_origin: Literal['run', 'restart']
    execution_status: Execution
    numerical_validation: Validation
    inlet_pressure: Pressure
    outlet_pressure: Pressure
    liquid_rate_source: LiquidRate
    liquid_rate_sink: LiquidRate
    profile: Profile
    repeatability: Literal['CONFIRMED', 'FAILED', 'NOT_ASSESSED']
    checks: dict[str, bool]


class Difference(StrictModel):
    value: FiniteFloat
    unit: Literal['bar'] = 'bar'
    base_object_id: Literal['agzu6_out'] = 'agzu6_out'
    plus10_object_id: Literal['agzu6_out'] = 'agzu6_out'


class Comparison(StrictModel):
    inlet_pressure_plus10_minus_base: Difference


class WarningMessage(StrictModel):
    scenario: Literal['base', 'plus10']
    stage: Literal['run', 'restart']
    message: str


class Error(StrictModel):
    code: Literal['INVALID_ARGUMENT', 'RUN_NOT_FOUND', 'REPORT_NOT_FOUND', 'REPORT_INVALID',
                  'INPUT_MISMATCH', 'RESULT_INCOMPLETE', 'CALCULATION_FAILED', 'TRACE_WRITE_FAILED']
    field: str | None
    message: str


class ReportRef(StrictModel):
    run_id: str = Field(pattern=UUID_PATTERN)
    artifact: Literal['report.json'] = 'report.json'


class StockConditions(StrictModel):
    temperature: Temperature
    pressure: Pressure


class ReadResponse(StrictModel):
    schema_version: Literal[1] = 1
    trace_id: str = Field(pattern=UUID_PATTERN)
    run_id: str | None = Field(pattern=UUID_PATTERN)
    scenario: Literal['base', 'plus10', 'compare'] | None
    tool_status: Literal['SUCCESS', 'ERROR']
    execution_status: Execution
    numerical_validation: Validation
    physical_validation: Literal['NOT_ASSESSED'] = 'NOT_ASSESSED'
    results: dict[Literal['base', 'plus10'], ScenarioResult]
    comparison: Comparison | None
    warnings: list[WarningMessage]
    report_ref: ReportRef | None
    stock_tank_conditions: StockConditions | None
    numerical_limits: dict[str, FiniteFloat]
    error: Error | None


INPUT_SCHEMA = ReadRequest.model_json_schema()
OUTPUT_SCHEMA = ReadResponse.model_json_schema()
