"""Common runtime types for cipher-2."""

from typing import Dict, List, Union

JSONValue = Union[None, bool, int, float, str, List["JSONValue"], Dict[str, "JSONValue"]]

__all__ = ["JSONValue"]
