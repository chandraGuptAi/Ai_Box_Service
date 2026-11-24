from .base import (Attachment, BaseEmailProvider, DraftResult, MailboxProfile, OAuthTokens, OutgoingEmail,
                   ProviderError)
from .gmail import GmailProvider
from .outlook import OutlookProvider

PROVIDER_CLASSES: dict[str, type[BaseEmailProvider]] = {"gmail": GmailProvider, "outlook": OutlookProvider}

__all__ = ["Attachment", "BaseEmailProvider", "DraftResult", "GmailProvider", "MailboxProfile", "OAuthTokens",
           "OutgoingEmail", "OutlookProvider", "PROVIDER_CLASSES", "ProviderError"]
