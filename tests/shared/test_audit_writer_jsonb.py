"""AuditWriter must send metadata as a JSON string + CAST(... AS jsonb). FAKE session."""
import json
from unittest.mock import AsyncMock
from uuid import uuid4

from shared.audit.writer import AuditWriter


async def test_metadata_is_json_string_with_jsonb_cast():
    session = AsyncMock()
    op = uuid4()
    await AuditWriter(session).log(
        "svc", "act", "entity", "e1", operator_id=op,
        metadata={"a": 1, "u": uuid4(), "nested": {"k": [1, 2]}},
    )
    stmt, params = session.execute.call_args.args
    assert "CAST(:metadata AS jsonb)" in str(stmt)
    assert isinstance(params["metadata"], str)  # asyncpg cannot take a dict for jsonb
    assert json.loads(params["metadata"])["nested"] == {"k": [1, 2]}
    assert params["operator_id"] == str(op)


async def test_missing_metadata_becomes_empty_json_object():
    session = AsyncMock()
    await AuditWriter(session).log("svc", "act", "entity", "e1")
    _, params = session.execute.call_args.args
    assert params["metadata"] == "{}"
