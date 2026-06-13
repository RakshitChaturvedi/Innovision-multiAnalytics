from unittest.mock import MagicMock, patch

import pytest
import redis

from scripts.create_consumer_groups import (
    STREAMS,
    create_groups,
)


@patch("scripts.create_consumer_groups.redis.Redis")
def test_create_all_groups(mock_redis):

    mock_instance = MagicMock()
    mock_redis.return_value = mock_instance

    create_groups()

    expected_calls = sum(
        len(groups)
        for groups in STREAMS.values()
    )

    assert (
        mock_instance.xgroup_create.call_count
        == expected_calls
    )


@patch("scripts.create_consumer_groups.redis.Redis")
def test_busygroup_is_ignored(mock_redis):

    mock_instance = MagicMock()

    mock_instance.xgroup_create.side_effect = (
        redis.exceptions.ResponseError(
            "BUSYGROUP Consumer Group name already exists"
        )
    )

    mock_redis.return_value = mock_instance

    create_groups()


@patch("scripts.create_consumer_groups.redis.Redis")
def test_unexpected_error_raises(mock_redis):

    mock_instance = MagicMock()

    mock_instance.xgroup_create.side_effect = (
        redis.exceptions.ResponseError(
            "something bad happened"
        )
    )

    mock_redis.return_value = mock_instance

    with pytest.raises(
        redis.exceptions.ResponseError
    ):
        create_groups()