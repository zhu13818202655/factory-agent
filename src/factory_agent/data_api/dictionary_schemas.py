# Customer field names mirror the reviewed external contract.
# ruff: noqa: N815

from collections.abc import Mapping
from math import isfinite
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

type StableId = Annotated[str, Field(min_length=1, max_length=128)]
type Count = Annotated[int, Field(ge=0, strict=True)]
type Switch = Annotated[int, Field(ge=0, le=1)]


class SafeDictionaryRow(BaseModel):
    """Only approved directory fields survive this external boundary."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def normalize_strings(cls, value: object, info: ValidationInfo) -> object:
        field = cls.model_fields.get(info.field_name or "")
        identifier = info.field_name in {
            "bh",
            "id",
            "huohao",
            "wt",
            "sctype",
            "dh",
            "bbreed",
            "xianhao",
        }
        if field is None or (field.annotation is not str and not identifier):
            return value
        if isinstance(value, bool):
            raise ValueError("directory text cannot be boolean")
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not isfinite(value):
                raise ValueError("directory numeric text must be finite")
            if identifier and (not value.is_integer() or abs(value) > 2**53):
                raise ValueError("directory numeric identifier must be an exact integer")
            return str(int(value)) if value.is_integer() else str(value)
        if value is None:
            return ""
        return value

    @field_validator("*", mode="after")
    @classmethod
    def validate_identifiers(cls, value: object, info: ValidationInfo) -> object:
        if info.field_name in {"bh", "id", "huohao", "wt", "sctype", "dh", "bbreed"}:
            if isinstance(value, str) and (not value.strip() or value != value.strip()):
                raise ValueError("directory identifier must be nonempty and canonical")
        return value


class StyleRow(SafeDictionaryRow):
    bh: StableId
    bbreed: StableId
    description: str
    stype: StableId
    dw: str


class StyleTypeRow(SafeDictionaryRow):
    bh: StableId
    pbh: str
    name: str
    isdelete: Switch


class StyleColorRow(SafeDictionaryRow):
    id: StableId
    bh: StableId
    color: str


class StyleSizeRow(SafeDictionaryRow):
    id: StableId
    bh: StableId
    chima: str
    banx: str = ""
    kez: str = ""


class ProductionTypeRow(SafeDictionaryRow):
    bh: StableId
    name: str
    sfjcj: Switch
    isdelete: Switch
    sfcprk: Switch


class ProcessRow(SafeDictionaryRow):
    bh: StableId
    name: str
    gxtype: int
    isdelete: Switch
    section: str
    wt_sort: str = ""
    vehicle_type: str = ""


class StyleProcessRow(SafeDictionaryRow):
    id: StableId
    huohao: StableId
    huohaoname: StableId
    wt: StableId
    wtname: str
    sort: int
    sctype: StableId
    sctypename: str
    using_state: Switch
    zhgx: Switch


class BillProcessRow(SafeDictionaryRow):
    id: StableId
    dh: StableId
    huohao: StableId
    huohaoname: StableId
    wt: StableId
    wtname: str
    sort: int
    sctype: StableId
    sctypename: str
    zhgx: Switch


class HangingLineRow(SafeDictionaryRow):
    id: StableId
    dgname: str
    xianhao: StableId
    zuBieName: str


class DictionaryResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    @classmethod
    def from_payload(cls, payload: object) -> "DictionaryResult":
        if not isinstance(payload, Mapping):
            raise ValueError("directory response must be an object")
        return cls.model_validate(cast(Mapping[str, object], payload))


class StyleDirectoryResult(DictionaryResult):
    huohaoList: tuple[StyleRow, ...]
    hh_total: Count
    huohaoTypeList: tuple[StyleTypeRow, ...]
    ht_total: Count


class StyleFormResult(DictionaryResult):
    huohaoList: tuple[StyleRow, ...]
    hh_total: Count
    huohaoColorList: tuple[StyleColorRow, ...]
    hc_total: Count
    huohaoChimaList: tuple[StyleSizeRow, ...]
    hs_total: Count


class ProductionTypeResult(DictionaryResult):
    sctypeList: tuple[ProductionTypeRow, ...]
    total: Count


class ProcessResult(DictionaryResult):
    worktypeList: tuple[ProcessRow, ...]
    total: Count


class StyleProcessResult(DictionaryResult):
    list: tuple[StyleProcessRow, ...]
    total: Count


class BillProcessResult(DictionaryResult):
    list: tuple[BillProcessRow, ...]
    total: Count


class HangingLineResult(DictionaryResult):
    list: tuple[HangingLineRow, ...]
    total: Count
