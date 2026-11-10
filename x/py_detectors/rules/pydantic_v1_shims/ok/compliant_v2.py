from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class User(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    name: str

    @field_validator("name")
    @classmethod
    def v(cls, v: str) -> str:
        return v

    @model_validator(mode="after")
    def check(self) -> "User":
        return self
