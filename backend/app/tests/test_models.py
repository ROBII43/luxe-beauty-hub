from backend.app.models import Category, Order, Product, Role, User


def test_core_models_are_registered() -> None:
    assert Category.__tablename__ == "categories"
    assert Product.__tablename__ == "products"
    assert Order.__tablename__ == "orders"
    assert Role.__tablename__ == "roles"
    assert User.__tablename__ == "users"