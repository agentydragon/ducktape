from pydantic import BaseModel


class User(BaseModel):
    class Config:
        validate_assignment = True
