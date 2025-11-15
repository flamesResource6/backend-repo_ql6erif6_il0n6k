"""
BizMate Database Schemas

Each Pydantic model below represents a MongoDB collection. The collection
name is the lowercase of the class name (e.g., Customer -> "customer").
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal, List
from datetime import datetime


class Customer(BaseModel):
    name: str = Field(..., description="Customer full name")
    phone: Optional[str] = Field(None, description="Phone number")
    email: Optional[str] = Field(None, description="Email address")
    address: Optional[str] = Field(None, description="Address")
    note: Optional[str] = Field(None, description="Extra notes about the customer")
    debt_balance: float = Field(0.0, ge=0, description="Outstanding due amount")


class Product(BaseModel):
    name: str = Field(..., description="Product name")
    unit_price: float = Field(..., ge=0, description="Unit price")
    quantity: int = Field(0, ge=0, description="Available quantity in stock")
    threshold: int = Field(0, ge=0, description="Low-stock threshold")


class SaleItem(BaseModel):
    product_id: str = Field(..., description="ID of product sold")
    product_name: str = Field(..., description="Cached product name at time of sale")
    quantity: int = Field(..., ge=1)
    price: float = Field(..., ge=0, description="Unit price at time of sale")


class Sale(BaseModel):
    customer_id: Optional[str] = Field(None, description="Linked customer ID if any")
    customer_name: Optional[str] = Field(None, description="Cached customer name")
    items: List[SaleItem] = Field(default_factory=list)
    payment_mode: Literal['Cash', 'UPI', 'Card', 'Other'] = 'Cash'
    total_amount: float = Field(..., ge=0)
    created_at: Optional[datetime] = None


class Setting(BaseModel):
    key: str
    value: str
    category: Optional[str] = None
