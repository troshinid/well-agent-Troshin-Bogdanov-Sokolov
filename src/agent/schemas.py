"""Closed task vocabulary: LLM cannot supply paths or arbitrary tool arguments."""

from pathlib import Path
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from src.well_agent_mcp.schemas import UUID_PATTERN, Execution, Validation


class Task(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['read_results', 'run_steady_state', 'refuse']
    scenario: Literal['base', 'plus10', 'compare']
    reason: Literal['SUPPORTED', 'OUT_OF_SCOPE', 'AMBIGUOUS', 'MODEL_CHANGE', 'UNTRUSTED_INSTRUCTION']

    @model_validator(mode='after')
    def coherent(self):
        if (self.action == 'refuse') == (self.reason == 'SUPPORTED'):
            raise ValueError('Inconsistent refusal')
        if self.action == 'run_steady_state' and self.scenario != 'compare':
            raise ValueError('Only the agreed pair can be rerun')
        return self


class AgentContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    registry: Path
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    run_id: str = Field(pattern=UUID_PATTERN, strict=True)
    request_id: str = Field(default_factory=lambda: str(uuid4()), pattern=UUID_PATTERN, strict=True)
    allow_run: bool = Field(default=False, strict=True)


class AgentResult(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    schema_version: Literal[1] = 1
    answer: str = ''
    status: Literal['SUCCESS', 'CHECK_REQUIRED', 'REFUSED', 'ERROR'] = 'ERROR'
    request_id: str = Field(pattern=UUID_PATTERN)
    trace_id: str = Field(pattern=UUID_PATTERN)
    task: Task | None = None
    execution_status: Execution = 'NOT_RUN'
    numerical_validation: Validation = 'NOT_ASSESSED'
    physical_validation: Literal['NOT_ASSESSED'] = 'NOT_ASSESSED'
    evidence: list[dict] = Field(default_factory=list)
    error: dict[str, str] | None = None

