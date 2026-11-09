"""Provider-neutral shopping list models."""

from dataclasses import asdict, dataclass, field
from typing import Any


class ConnectorError(Exception):
    """A fixed, user-facing error; never include credentials or remote bodies."""


class AuthenticationRequired(ConnectorError):
    """The user needs to reconnect the account."""


class UnsupportedOperation(ConnectorError):
    """An operation isn't supported by this particular provider item."""


@dataclass(frozen=True)
class ShoppingList:
    id: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Item:
    uid: str
    name: str
    description: str = ""
    completed: bool = False
    editable: bool = True
    deletable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fields(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "completed": self.completed}
