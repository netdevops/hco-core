"""Identifier types.

Distinct `NewType`s rather than bare `str`, so a type checker catches passing a
run id where a device id belongs. That mistake is cheap to make and expensive to
find, because both are opaque strings at runtime.
"""

from typing import NewType

DeviceID = NewType("DeviceID", str)
RunID = NewType("RunID", str)
ChangeSetID = NewType("ChangeSetID", str)
TenantID = NewType("TenantID", str)
PromptVersionID = NewType("PromptVersionID", str)
BindingID = NewType("BindingID", str)
