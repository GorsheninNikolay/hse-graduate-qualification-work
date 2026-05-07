"""5-class public error contract for the DSL loader (roadmap section 4 line 107)."""

from dataclasses import dataclass
from enum import StrEnum


class DSLErrorCode(StrEnum):
    YAML_PARSE = "yaml.parse"
    YAML_SCHEMA = "yaml.schema"
    REF_PROFILE = "ref.profile"
    REF_RULE = "ref.rule"
    REF_OPERATION = "ref.operation"


@dataclass(frozen=True, slots=True)
class DSLError(Exception):
    code: DSLErrorCode
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code.value} at {self.path or '<root>'}: {self.message}"
