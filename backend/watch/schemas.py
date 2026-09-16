from ninja import Schema
from pydantic import Field

MAX_PROGRESS_SECONDS = 86_400


class ProgressIn(Schema):
    current_time: float = Field(ge=0, le=MAX_PROGRESS_SECONDS, allow_inf_nan=False)
    duration: float = Field(ge=0, le=MAX_PROGRESS_SECONDS, allow_inf_nan=False)


class ProgressOut(Schema):
    current_time: float
    duration: float
    percentage: float
