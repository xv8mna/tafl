"""Exceptions shared by the engine and plugins."""


class TaflError(Exception):
    """Base class for errors whose message is meant for the user."""


class ActionError(TaflError):
    """A problem the user can fix: bad settings, a missing column, an unreadable file.

    Actions raise this with a plain-language message; it is shown on the step without a traceback.
    """


class Cancelled(TaflError):
    """The user cancelled the calculation."""

    def __init__(self, message: str = "Cancelled") -> None:
        super().__init__(message)
