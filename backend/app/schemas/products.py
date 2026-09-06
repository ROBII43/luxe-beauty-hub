from decimal import Decimal

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=180)
    brand: str | None = None
    category_id: int | None = None
    description: str | None = None
    price: Decimal = Field(ge=0)
    discount_price: Decimal | None = Field(default=None, ge=0)
    stock: int = Field(default=0, ge=0)
    minimum_stock: int = Field(default=5, ge=0)
    image_url: str | None = None
    featured: bool = False
    active: bool = True


class ProductUpdate(ProductCreate):
    pass


class ProductResponse(ProductCreate):
    id: int

    model_config = {"from_attributes": True}


class ProductListResponse(BaseModel):
    items: list[ProductResponse]
    page: int
    per_page: int
    total: int
