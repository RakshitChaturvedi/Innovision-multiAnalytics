import structlog


# Configures Logger for future logging.
def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )


# Gets logger object to log details.
logger = structlog.get_logger()