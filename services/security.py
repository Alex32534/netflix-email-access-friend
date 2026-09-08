import logging

from cryptography.fernet import Fernet, InvalidToken

import config

logger = logging.getLogger(__name__)


def _fernet():
    if not config.IMAP_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(config.IMAP_ENCRYPTION_KEY.encode("utf-8"))
    except (ValueError, TypeError):
        logger.error("IMAP_ENCRYPTION_KEY is not a valid Fernet key")
        return None


def encrypt_secret(value):
    cipher = _fernet()
    if not cipher or not value:
        return None
    return cipher.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    cipher = _fernet()
    if not cipher or not value:
        return None
    try:
        return cipher.decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        logger.error("Unable to decrypt the stored IMAP credential")
        return None
