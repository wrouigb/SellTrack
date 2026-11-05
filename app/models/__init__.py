"""Database models. Importing this package registers every table on the metadata."""

from app.models.audit_log import AuditLog
from app.models.feature_flag import FeatureFlag
from app.models.notification import Notification
from app.models.partner import Partner
from app.models.payment import PartnerPayment
from app.models.product import Product, ProductCostChange
from app.models.sale import Sale
from app.models.settlement import Settlement
from app.models.support_message import SupportMessage, SupportStatus
from app.models.upfront_cost import CostCategory, UpfrontCost
from app.models.user import (
    DEFAULT_TIMEZONE,
    RecoveryCode,
    SignIn,
    Theme,
    User,
    UserRole,
    UserStatus,
)

__all__ = [
    "DEFAULT_TIMEZONE",
    "AuditLog",
    "CostCategory",
    "FeatureFlag",
    "Notification",
    "Partner",
    "PartnerPayment",
    "Product",
    "ProductCostChange",
    "RecoveryCode",
    "Sale",
    "Settlement",
    "SignIn",
    "SupportMessage",
    "SupportStatus",
    "Theme",
    "UpfrontCost",
    "User",
    "UserRole",
    "UserStatus",
]
