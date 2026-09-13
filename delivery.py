from decimal import Decimal, InvalidOperation


DEFAULT_SHIPPING_CLASS = "Standard"
SHIPPING_CLASSES = ("Standard", "Heavy", "Fragile", "Large Appliance", "Free Delivery", "Pickup Only")


def decimal_value(value, default="0"):
    try:
        return max(Decimal(str(value)), Decimal("0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def delivery_settings(shop):
    settings = shop.setdefault("delivery", {})
    settings.setdefault("enabled", True)
    settings.setdefault("method", "zone")
    settings.setdefault("free_delivery_enabled", True)
    settings.setdefault("default_free_threshold", shop.get("free_delivery_threshold", 10000))
    settings.setdefault("default_fee", shop.get("delivery_fee", 350))
    settings.setdefault("zones", [])
    return settings


def product_shipping_class(product):
    return product.get("shipping_class", DEFAULT_SHIPPING_CLASS) or DEFAULT_SHIPPING_CLASS


def order_weight(items):
    return sum(decimal_value(item.get("weight_kg", 0.5)) * int(item.get("quantity", 0)) for item in items)


def matching_zone(settings, location):
    normalized = str(location or "").strip().casefold()
    if not normalized:
        return None
    for zone in settings.get("zones", []):
        if not zone.get("active", True):
            continue
        locations = zone.get("locations", [])
        if isinstance(locations, str):
            locations = [part.strip() for part in locations.split(",")]
        if any(str(place).strip().casefold() in normalized or normalized in str(place).strip().casefold() for place in locations if str(place).strip()):
            return zone
    return None


def calculate_delivery(settings, location, subtotal, items):
    subtotal = decimal_value(subtotal)
    if not settings.get("enabled", True):
        return {"fee": Decimal("0"), "zone": None, "reason": "Delivery disabled"}
    zone = matching_zone(settings, location)
    if not zone:
        zone = {
            "name": "Other Kenya",
            "base_fee": settings.get("default_fee", 350),
            "free_threshold": settings.get("default_free_threshold", 0),
            "calculation_type": "fixed",
            "active": True,
        }
    classes = {product_shipping_class(item) for item in items}
    if "Pickup Only" in classes:
        return {"fee": Decimal("0"), "zone": zone, "reason": "Pickup only"}
    if "Free Delivery" in classes:
        return {"fee": Decimal("0"), "zone": zone, "reason": "Free delivery item"}
    class_fees = zone.get("shipping_class_fees", {})
    base_fee = max((decimal_value(class_fees.get(shipping_class, zone.get("base_fee", 0))) for shipping_class in classes), default=decimal_value(zone.get("base_fee", 0)))
    calculation_type = zone.get("calculation_type", settings.get("method", "zone"))
    if calculation_type == "weight":
        first_weight = decimal_value(zone.get("first_weight_kg", 2), "2")
        additional_fee = decimal_value(zone.get("additional_weight_fee", 0))
        weight = order_weight(items)
        extra_units = max(Decimal("0"), (weight - first_weight).to_integral_value(rounding="ROUND_CEILING"))
        fee = base_fee + extra_units * additional_fee
    else:
        fee = base_fee
    if settings.get("free_delivery_enabled", True) and subtotal >= decimal_value(zone.get("free_threshold", settings.get("default_free_threshold", 0))):
        fee = Decimal("0")
        reason = "Free delivery threshold"
    else:
        reason = "Zone fee"
    return {"fee": fee.quantize(Decimal("0.01")), "zone": zone, "reason": reason}