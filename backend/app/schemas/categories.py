from pydantic import BaseModel, ConfigDict


class CategoryResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    image_url: str | None = None
    parent_id: int | None = None
    sort_order: int = 0

    model_config = ConfigDict(from_attributes=True)