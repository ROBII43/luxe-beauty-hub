from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.database.session import get_db
from backend.app.models.catalog import Category
from backend.app.schemas.categories import CategoryResponse

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get("", response_model=list[CategoryResponse])
def list_categories(db: Session = Depends(get_db)) -> list[Category]:
    return list(db.scalars(select(Category).where(Category.active.is_(True)).order_by(Category.sort_order, Category.name)).all())