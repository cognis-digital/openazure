"""openazure - a local, open-source reimplementation of core Azure primitives.

openazure provides local, in-process emulations of Azure Blob Storage,
Table Storage, Queue Storage, and an Azure-Functions-style runner, for
local development, testing, and offline work.

This project is an independent open reimplementation and is NOT affiliated
with, endorsed by, or sponsored by Microsoft. "Azure" is used only
nominatively to describe API compatibility. It implements a compatible
subset and is not intended for production use.
"""

__version__ = "0.1.0"

from .blob import BlobService
from .table import TableService
from .queue import QueueService
from .functions import FunctionRunner

__all__ = [
    "BlobService",
    "TableService",
    "QueueService",
    "FunctionRunner",
    "__version__",
]
