"""Platform adapters for the Meta Graph APIs."""
from .base import BaseMetaProvider
from .facebook import FacebookProvider
from .graph import GraphClient, GraphError
from .instagram import InstagramProvider
from .whatsapp import WhatsAppProvider

PROVIDER_CLASSES: dict[str, type[BaseMetaProvider]] = {
    "whatsapp": WhatsAppProvider, "instagram": InstagramProvider, "facebook": FacebookProvider}

__all__ = ["BaseMetaProvider", "FacebookProvider", "GraphClient", "GraphError", "InstagramProvider",
           "PROVIDER_CLASSES", "WhatsAppProvider"]
