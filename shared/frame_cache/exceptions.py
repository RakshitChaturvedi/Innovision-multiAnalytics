class FrameCacheError(Exception):
    """Base exception for all frame cache errors"""

class FrameCacheConnectionError(FrameCacheError):
    """When cache cant be reached"""

class FrameCacheOperationError(FrameCacheError):
    """When cache operation fails"""