"""Shared typing helpers."""

from __future__ import annotations

import enum
from typing import Literal, TypeVar, Union

__all__ = ["OMIT", "Omit", "Omittable"]


class Omit(enum.Enum):
    """The type of :data:`OMIT`: a parameter left out, as opposed to ``None``, which sends ``null``."""

    OMIT = enum.auto()

    def __repr__(self) -> str:
        return "OMIT"

    def __bool__(self) -> Literal[False]:
        return False


#: Leaves a field out of the request. Where ``None`` is meaningful, for example
#: ``billing_tag=None`` clears a tag, the default is ``OMIT`` instead.
OMIT = Omit.OMIT

T = TypeVar("T")
Omittable = Union[T, Omit]
