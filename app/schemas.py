from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    created_at: datetime

    class Config:
        from_attributes = True


class PredictionOut(BaseModel):
    id: int
    created_at: datetime
    result_payload: Optional[dict]
    input_payload: Optional[dict]

    class Config:
        from_attributes = True
