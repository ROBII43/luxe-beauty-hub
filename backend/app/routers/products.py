from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.app.core.audit import record_audit
from backend.app.core.dependencies import require_permission
from backend.app.database.session import get_db
from backend.app.models.catalog import Product
from backend.app.schemas.products import ProductCreate, ProductListResponse, ProductResponse, ProductUpdate

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("/{product_id}", response_model=ProductResponse)
def get_product(product_id: int, db: Session = Depends(get_db)) -> Product:
    product = db.get(Product, product_id)
    if not product or not product.active:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.get("", response_model=ProductListResponse)
def list_products(
    db: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=24, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=120),
    category_id: int | None = Query(default=None, ge=1),
    brand: str | None = Query(default=None, min_length=1, max_length=120),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    in_stock: bool | None = None,
    sort: str = Query(default="latest", pattern="^(latest|price_asc|price_desc|name)$"),
) -> ProductListResponse:
    filters = [Product.active.is_(True)]
    if search:
        term = f"%{search.strip()}%"
        filters.append(or_(Product.name.ilike(term), Product.brand.ilike(term), Product.sku.ilike(term)))
    if category_id is not None: filters.append(Product.category_id == category_id)
    if brand: filters.append(Product.brand.ilike(brand.strip()))
    if min_price is not None: filters.append(Product.price >= min_price)
    if max_price is not None: filters.append(Product.price <= max_price)
    if in_stock is True: filters.append(Product.stock > 0)
    if in_stock is False: filters.append(Product.stock == 0)
    query = select(Product).where(*filters)
    if sort == "price_asc": query = query.order_by(Product.price.asc())
    elif sort == "price_desc": query = query.order_by(Product.price.desc())
    elif sort == "name": query = query.order_by(Product.name.asc())
    else: query = query.order_by(Product.created_at.desc(), Product.id.desc())
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    products = db.scalars(query.offset((page - 1) * per_page).limit(per_page)).all()
    return ProductListResponse(items=products, page=page, per_page=per_page, total=total)


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, db: Session = Depends(get_db), claims: dict = Depends(require_permission("products:write"))) -> Product:
    if db.scalar(select(Product).where(Product.sku == payload.sku)):
        raise HTTPException(status_code=409, detail="SKU already exists")
    product = Product(**payload.model_dump())
    db.add(product)
    db.flush()
    record_audit(db, claims, "product_created", "product", f"Product {product.name} created", str(product.id))
    db.commit()
    db.refresh(product)
    return product


@router.put("/{product_id}", response_model=ProductResponse)
def update_product(product_id: int, payload: ProductUpdate, db: Session = Depends(get_db), claims: dict = Depends(require_permission("products:write"))) -> Product:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    for key, value in payload.model_dump().items():
        setattr(product, key, value)
    record_audit(db, claims, "product_updated", "product", f"Product {product.name} updated", str(product.id))
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(product_id: int, db: Session = Depends(get_db), claims: dict = Depends(require_permission("products:write"))) -> None:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.active = False
    record_audit(db, claims, "product_deleted", "product", f"Product {product.name} deactivated", str(product.id))
    db.commit()
