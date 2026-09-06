from datetime import datetime
from decimal import Decimal
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.dependencies import current_claims, require_admin
from backend.app.core.audit import record_audit
from backend.app.database.session import get_db
from backend.app.models.catalog import Product
from backend.app.models.orders import InventoryMovement, Order, OrderItem, Payment
from backend.app.schemas.orders import OrderCreate, OrderResponse

router = APIRouter(prefix="/api/orders", tags=["orders"])


def user_id_from_claims(claims: dict) -> int:
    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="Invalid user identity") from error


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(payload: OrderCreate, db: Session = Depends(get_db), claims: dict = Depends(current_claims)) -> Order:
    customer_id = user_id_from_claims(claims)
    quantities: dict[int, int] = {}
    for item in payload.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity

    with db.begin():
        products = db.scalars(select(Product).where(Product.id.in_(quantities), Product.active.is_(True)).with_for_update()).all()
        by_id = {product.id: product for product in products}
        if len(by_id) != len(quantities):
            raise HTTPException(status_code=400, detail="One or more products are unavailable")
        if any(product.stock < quantities[product.id] for product in products):
            raise HTTPException(status_code=409, detail="Insufficient stock for one or more products")

        subtotal = sum((Decimal(product.price) * quantities[product.id] for product in products), Decimal("0"))
        delivery_fee = Decimal("0") if subtotal >= Decimal("10000") else Decimal("350")
        order = Order(
            order_number=f"ORD-{datetime.utcnow():%Y%m%d}-{secrets.token_hex(3).upper()}",
            customer_id=customer_id,
            status="Pending",
            payment_method=payload.payment_method,
            payment_status="Pending",
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total=subtotal + delivery_fee,
            delivery_address=payload.delivery_address,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(order)
        db.flush()
        db.add(Payment(order_id=order.id, method=payload.payment_method, status="Pending", amount=order.total, created_at=datetime.utcnow()))
        record_audit(db, claims, "order_created", "order", f"Order {order.order_number} created", str(order.id))
        for product in products:
            quantity = quantities[product.id]
            previous_stock = product.stock
            product.stock -= quantity
            db.add(OrderItem(order_id=order.id, product_id=product.id, product_name=product.name, quantity=quantity, unit_price=product.price, total_price=Decimal(product.price) * quantity))
            db.add(InventoryMovement(product_id=product.id, quantity_change=-quantity, previous_stock=previous_stock, new_stock=product.stock, reason=f"Order {order.order_number}", user_id=customer_id, created_at=datetime.utcnow()))
    db.refresh(order)
    return order


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: int, db: Session = Depends(get_db), claims: dict = Depends(current_claims)) -> Order:
    order = db.get(Order, order_id)
    if not order or (claims.get("role") not in {"SUPERADMIN", "ADMIN", "MANAGER"} and order.customer_id != user_id_from_claims(claims)):
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("", response_model=list[OrderResponse], dependencies=[Depends(require_admin)])
def list_orders(db: Session = Depends(get_db)) -> list[Order]:
    return list(db.scalars(select(Order).order_by(Order.created_at.desc())).all())