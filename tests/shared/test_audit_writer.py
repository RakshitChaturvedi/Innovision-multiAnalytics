from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from shared.audit.writer import AuditWriter


@pytest.fixture
def mock_session():
    session = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_log_executes_insert(mock_session):
    writer = AuditWriter(mock_session)

    await writer.log(
        service="recognition",
        action="embedding_written",
        entity_type="face_embedding",
        entity_id="123"
    )

    mock_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_log_does_not_commit(mock_session):
    writer = AuditWriter(mock_session)

    await writer.log(
        service="recognition",
        action="embedding_written",
        entity_type="face_embedding",
        entity_id="123"
    )

    assert not mock_session.commit.called


@pytest.mark.asyncio
async def test_log_defaults_metadata_to_empty_dict(mock_session):
    writer = AuditWriter(mock_session)

    await writer.log(
        service="test",
        action="test",
        entity_type="entity",
        entity_id="1"
    )

    params = mock_session.execute.call_args.args[1]

    assert params["metadata"] == {}


@pytest.mark.asyncio
async def test_log_preserves_metadata(mock_session):
    writer = AuditWriter(mock_session)

    metadata = {
        "camera_id": "cam1",
        "track_id": 42,
    }

    await writer.log(
        service="test",
        action="test",
        entity_type="entity",
        entity_id="1",
        metadata=metadata
    )

    params = mock_session.execute.call_args.args[1]

    assert params["metadata"] == metadata


@pytest.mark.asyncio
async def test_log_stores_operator_id(mock_session):
    writer = AuditWriter(mock_session)

    operator_id = uuid4()

    await writer.log(
        service="test",
        action="test",
        entity_type="entity",
        entity_id="1",
        operator_id=operator_id
    )

    params = mock_session.execute.call_args.args[1]

    assert params["operator_id"] == str(operator_id)


@pytest.mark.asyncio
async def test_log_none_operator_id(mock_session):
    writer = AuditWriter(mock_session)

    await writer.log(
        service="test",
        action="test",
        entity_type="entity",
        entity_id="1"
    )

    params = mock_session.execute.call_args.args[1]

    assert params["operator_id"] is None