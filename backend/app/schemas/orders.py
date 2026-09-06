from decimal import Decimal

from pydantic import BaseModel, Field


class OrderItemCreate(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=100)


class OrderCreate(BaseModel):
    items: list[OrderItemCreate] = Field(min_length=1, max_length=100)
    delivery_address: str = Field(min_length=5, max_length=1000)
    payment_method: str = Field(pattern="^(M-Pesa|Cash on Delivery)$")


class OrderItemResponse(BaseModel):
    product_id: int
    product_name: str
    quantity: int
    unit_price: Decimal
    total_price: Decimal

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: int
    order_number: str
    status: str
    payment_method: str | None
    payment_status: str
    subtotal: Decimal
    delivery_fee: Decimal
    total: Decimal
    delivery_address: str | None
    items: list[OrderItemResponse]

    model_config = {"from_attributes": True}