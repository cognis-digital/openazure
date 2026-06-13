import pytest

from openazure.store import Store
from openazure.blob import BlobService
from openazure.table import TableService
from openazure.queue import QueueService
from openazure.functions import FunctionRunner


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def blob(store):
    return BlobService(store)


@pytest.fixture
def table(store):
    return TableService(store)


@pytest.fixture
def queue(store):
    return QueueService(store)


@pytest.fixture
def functions(queue):
    return FunctionRunner(queue)
