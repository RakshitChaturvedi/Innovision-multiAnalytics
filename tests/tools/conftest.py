import pytest

from tests.tools.registry_stub import RegistryStub


@pytest.fixture
def registry():
    with RegistryStub() as stub:
        yield stub
