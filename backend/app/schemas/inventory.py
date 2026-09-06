from datetime import datetime

from pydantic import BaseModel, Field


class InventoryAdjustment(BaseModel):
    quantity: int = Field(description="Positive to add stock, negative to reduce stock")
    reason: str = Field(min_length=2, max_length=180)


class InventoryResponse(BaseModel):
    product_id: int
    stock: int
    minimum_stock: int
    status: str


class InventoryMovementResponse(BaseModel):
    id: int
    product_id: int
    quantity_change: int
    previous_stock: int
    new_stock: int
    reason: str | None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
