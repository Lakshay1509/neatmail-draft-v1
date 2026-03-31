"""
providers/__init__.py — Provider factory.
"""

from providers.base import BaseEmailProvider
from providers.gmail import GmailProvider
from providers.outlook import OutlookProvider


def get_provider(is_gmail: bool, token: str, user_id: str) -> BaseEmailProvider:
    """
    Factory: return the correct email provider based on the is_gmail flag.

    Args:
        is_gmail: True → GmailProvider, False → OutlookProvider.
        token:    OAuth 2.0 access token for the relevant service.
        user_id:  The application-level user identifier.
    """
    if is_gmail:
        return GmailProvider(token=token, user_id=user_id)
    return OutlookProvider(token=token, user_id=user_id)
