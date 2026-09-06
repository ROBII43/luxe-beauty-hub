from decimal import Decimal

from pydantic import BaseModel, Field


class MpesaInitiateRequest(BaseModel):
    phone_number: str = Field(pattern=r"^\+?254[17]\d{8}$")


class MpesaInitiateResponse(BaseModel):
    order_id: int
    checkout_request_id: str
    customer_message: str
    amount: Decimal