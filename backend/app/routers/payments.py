import json
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.dependencies import current_claims
from backend.app.database.session import get_db
from backend.app.models.orders import Order, Payment
from backend.app.schemas.payments import MpesaInitiateRequest, MpesaInitiateResponse
from backend.app.services.mpesa import initiate_stk_push

router = APIRouter(prefix="/api/payments/mpesa", tags=["payments"])


@router.post("/stk-push", response_model=MpesaInitiateResponse)
def start_stk_push(order_id: int, payload: MpesaInitiateRequest, db: Session = Depends(get_db), claims: dict = Depends(current_claims)) -> MpesaInitiateResponse:
    try:
        customer_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="Invalid user identity") from error
    order = db.get(Order, order_id)
    if not order or order.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="Order not found")
    payment = db.scalar(select(Payment).where(Payment.order_id == order.id, Payment.method == "M-Pesa", Payment.status == "Pending").order_by(Payment.id.desc()))
    if not payment:
        raise HTTPException(status_code=409, detail="No pending M-Pesa payment exists for this order")
    try:
        provider_response = initiate_stk_push(payload.phone_number, int(Decimal(order.total)), order.order_number, db)
    except Exception as error:
        raise HTTPException(status_code=502, detail="M-Pesa payment request failed") from error
    request_id = provider_response.get("CheckoutRequestID")
    if not request_id:
        raise HTTPException(status_code=502, detail="M-Pesa returned no checkout request ID")
    payment.provider = "M-Pesa"
    payment.provider_request_id = request_id
    payment.callback_payload = json.dumps({"initiation": provider_response})
    db.commit()
    return MpesaInitiateResponse(order_id=order.id, checkout_request_id=request_id, customer_message=provider_response.get("CustomerMessage", "Check your phone to complete payment"), amount=order.total)


@router.post("/callback", status_code=status.HTTP_204_NO_CONTENT)
async def mpesa_callback(request: Request, db: Session = Depends(get_db)) -> None:
    payload = await request.json()
    callback = payload.get("Body", {}).get("stkCallback", {})
    request_id = callback.get("CheckoutRequestID")
    if not request_id:
        return
    payment = db.scalar(select(Payment).where(Payment.provider_request_id == request_id).with_for_update())
    if not payment or payment.status != "Pending":
        return
    result_code = callback.get("ResultCode")
    payment.status = "Paid" if result_code == 0 else "Failed"
    payment.callback_payload = json.dumps(payload)
    payment.provider_receipt = next((item.get("Value") for item in callback.get("CallbackMetadata", {}).get("Item", []) if item.get("Name") == "MpesaReceiptNumber"), None)
    payment.order.payment_status = payment.status
    if payment.status == "Paid":
        payment.order.status = "Confirmed"
    payment.created_at = payment.created_at or datetime.utcnow()
    db.commit()