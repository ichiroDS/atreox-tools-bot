"""Maps internal failures to friendly user-facing copy.

Users never see a stack trace or an internal error code; the code is only
persisted on the job row and written to the log.
"""

from __future__ import annotations

from app.bot import texts
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.telegram_files import FileTooLargeError

_MESSAGES: dict[ProcessingErrorCode, str] = {
    ProcessingErrorCode.UNSUPPORTED: texts.ERROR_UNSUPPORTED,
    ProcessingErrorCode.TOO_LARGE: texts.ERROR_TOO_LARGE,
}


def user_message(error: BaseException) -> str:
    if isinstance(error, FileTooLargeError):
        return texts.ERROR_TOO_LARGE
    if isinstance(error, MediaProcessingError):
        return _MESSAGES.get(error.code, texts.ERROR_PROCESSING)
    return texts.ERROR_PROCESSING


def error_code(error: BaseException) -> str:
    if isinstance(error, FileTooLargeError):
        return ProcessingErrorCode.TOO_LARGE.value
    if isinstance(error, MediaProcessingError):
        return error.code.value
    return ProcessingErrorCode.UNKNOWN.value
