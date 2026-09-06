from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    order_number = Column(String(40), unique=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(40), default="Pending")
    payment_method = Column(String(60))
    payment_status = Column(String(40), default="Pending")
    subtotal = Column(Numeric(12, 2), nullable=False)
    delivery_fee = Column(Numeric(12, 2), default=0)
    total = Column(Numeric(12, 2), nullable=False)
    delivery_address = Column(Text)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"))
    product_id = Column(Integer, ForeignKey("products.id"))
    product_name = Column(String(180), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    total_price = Column(Numeric(12, 2), nullable=False)
    order = relationship("Order", back_populates="items")


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"))
    method = Column(String(60), nullable=False)
    status = Column(String(40), default="Pending")
    transaction_reference = Column(String(180))
    provider = Column(String(40))
    provider_request_id = Column(String(180), unique=True)
    provider_receipt = Column(String(180))
    callback_payload = Column(Text)
    amount = Column(Numeric(12, 2))
    created_at = Column(DateTime)
    order = relationship("Order", back_populates="payments")


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    quantity_change = Column(Integer, nullable=False)
    previous_stock = Column(Integer, nullable=False)
    new_stock = Column(Integer, nullable=False)
    reason = Column(String(180), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, nullable=False)
