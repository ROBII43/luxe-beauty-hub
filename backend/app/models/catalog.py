from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import relationship

from backend.app.database.session import Base


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(String(255))
    image_url = Column(Text)
    parent_id = Column(Integer, ForeignKey("categories.id", ondelete="CASCADE"))
    active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    parent = relationship("Category", remote_side=[id], back_populates="children", uselist=False)
    children = relationship("Category", back_populates="parent")
    products = relationship("Product", back_populates="category")


class Brand(Base):
    __tablename__ = "brands"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    slug = Column(String(140), unique=True)
    description = Column(Text)
    logo_url = Column(Text)
    active = Column(Boolean, default=True)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String(80), unique=True, nullable=False)
    name = Column(String(180), nullable=False)
    brand = Column(String(120))
    category_id = Column(Integer, ForeignKey("categories.id"))
    description = Column(Text)
    price = Column(Numeric(12, 2), nullable=False)
    discount_price = Column(Numeric(12, 2))
    stock = Column(Integer, default=0, nullable=False)
    minimum_stock = Column(Integer, default=5, nullable=False)
    rating = Column(Numeric(2, 1), default=0)
    image_url = Column(Text)
    featured = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    category = relationship("Category", back_populates="products")
    images = relationship("ProductImage", back_populates="product", cascade="all, delete-orphan")
    variants = relationship("ProductVariant", back_populates="product", cascade="all, delete-orphan")


class ProductImage(Base):
    __tablename__ = "product_images"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"))
    image_url = Column(Text, nullable=False)
    sort_order = Column(Integer, default=0)
    primary = Column("is_primary", Boolean, default=False)
    product = relationship("Product", back_populates="images")


class ProductVariant(Base):
    __tablename__ = "product_variants"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"))
    name = Column(String(120))
    sku = Column(String(80), unique=True)
    price = Column(Numeric(12, 2))
    stock = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    product = relationship("Product", back_populates="variants")
