from typing import Any


class Service:
    """Business logic lives here, kept separate from HTTP routing."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def echo(self, message: str) -> str:
        return message
