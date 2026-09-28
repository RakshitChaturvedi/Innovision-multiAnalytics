class PermanentError(Exception):
    """Failure that retrying can never fix (bad payload, expired frame).

    BaseStreamConsumer copies the message to `{stream}:dlq` and ACKs it.
    """
