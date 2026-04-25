"""
backend/models/__init__.py

Imports all ORM models so Alembic can discover them.
"""

from backend.models.users         import User
from backend.models.depots        import Depot
from backend.models.products      import Product
from backend.models.batches       import Batch
from backend.models.stock         import StockMovement
from backend.models.retailers     import Retailer
from backend.models.invoices      import Invoice, InvoiceLineItem
from backend.models.payments      import Payment
from backend.models.returns       import Return, CreditNote
from backend.models.temperature   import TemperatureLog, IoTDevice
from backend.models.compliance    import Recall, AuditTrail, DiscountScheme
from backend.models.notifications import Notification
from backend.models.ml            import (
    DemandPrediction, ExpiryPrediction, FefoRanking,
    AgentAction, ModelRegistry, PipelineRunLog,
    DriftLog, SystemAlert,
)

__all__ = [
    "User", "Depot", "Product", "Batch",
    "StockMovement", "Retailer",
    "Invoice", "InvoiceLineItem",
    "Payment", "Return", "CreditNote",
    "TemperatureLog", "IoTDevice",
    "Recall", "AuditTrail", "DiscountScheme",
    "Notification",
    "DemandPrediction", "ExpiryPrediction", "FefoRanking",
    "AgentAction", "ModelRegistry", "PipelineRunLog",
    "DriftLog", "SystemAlert",
]