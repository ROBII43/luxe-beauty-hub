from io import BytesIO, StringIO
import csv

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.dependencies import require_permission
from backend.app.database.session import get_db
from backend.app.models.catalog import Product
from backend.app.models.orders import Order, OrderItem
from backend.app.models.users import User

router = APIRouter(prefix="/api/admin/reports", tags=["reports"])
REPORTS = {"sales", "products", "inventory", "customers", "orders"}


def rows_for(report: str, db: Session) -> tuple[list[str], list[list[str]]]:
    if report == "sales":
        header = ["Order", "Status", "Payment", "Subtotal", "Delivery", "Total", "Date"]
        rows = [[order.order_number, order.status, order.payment_status, str(order.subtotal), str(order.delivery_fee), str(order.total), str(order.created_at)] for order in db.scalars(select(Order).order_by(Order.created_at.desc())).all()]
    elif report == "products":
        header = ["ID", "SKU", "Name", "Brand", "Category ID", "Price", "Discount price", "Active"]
        rows = [[str(product.id), product.sku, product.name, product.brand or "", str(product.category_id or ""), str(product.price), str(product.discount_price or ""), str(product.active)] for product in db.scalars(select(Product).order_by(Product.name)).all()]
    elif report == "inventory":
        header = ["ID", "SKU", "Product", "Stock", "Minimum stock", "Status"]
        rows = [[str(product.id), product.sku, product.name, str(product.stock), str(product.minimum_stock), "OUT OF STOCK" if product.stock == 0 else "LOW STOCK" if product.stock <= product.minimum_stock else "IN STOCK"] for product in db.scalars(select(Product).order_by(Product.stock)).all()]
    elif report == "customers":
        header = ["ID", "Name", "Email", "Phone", "Active", "Registered"]
        rows = [[str(user.id), user.name, user.email, user.phone or "", str(user.active), str(user.created_at)] for user in db.scalars(select(User).order_by(User.name)).all()]
    else:
        header = ["Order", "Customer ID", "Status", "Payment", "Total", "Created"]
        rows = [[order.order_number, str(order.customer_id), order.status, order.payment_status, str(order.total), str(order.created_at)] for order in db.scalars(select(Order).order_by(Order.created_at.desc())).all()]
    return header, rows


def csv_bytes(header: list[str], rows: list[list[str]]) -> BytesIO:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    return BytesIO(output.getvalue().encode("utf-8-sig"))


def xlsx_bytes(header: list[str], rows: list[list[str]]) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows: sheet.append(row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]: cell.font = cell.font.copy(bold=True)
    output = BytesIO(); workbook.save(output); output.seek(0)
    return output


def pdf_bytes(header: list[str], rows: list[list[str]]) -> BytesIO:
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(A4), rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=24)
    table = Table([header] + rows, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3f5548")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dfe5dc")), ("FONTSIZE", (0, 0), (-1, -1), 7), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    document.build([table]); output.seek(0)
    return output


@router.get("/{report}.{extension}")
def export_report(report: str, extension: str, db: Session = Depends(get_db), _: dict = Depends(require_permission("reports:read"))) -> StreamingResponse:
    if report not in REPORTS or extension not in {"csv", "xlsx", "pdf"}:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Report format not found")
    header, rows = rows_for(report, db)
    builders = {"csv": (csv_bytes, "text/csv"), "xlsx": (xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), "pdf": (pdf_bytes, "application/pdf")}
    content, media_type = builders[extension]
    return StreamingResponse(content(header, rows), media_type=media_type, headers={"Content-Disposition": f"attachment; filename={report}-report.{extension}"})