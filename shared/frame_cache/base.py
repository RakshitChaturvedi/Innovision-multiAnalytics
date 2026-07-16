from abc import ABC, abstractmethod

class FrameCache(ABC):
    @abstractmethod
    async def put(
        self,
        frame_reference: str,
        frame_bytes: bytes,
        ttl_seconds: int,
    ) -> None:
        ...
    
    @abstractmethod
    async def get(
        self, 
        frame_reference: str,
    ) -> bytes | None:
        ...

    @abstractmethod
    async def delete(
        self, 
        frame_reference: str,
    ) -> None:
        ...

    @abstractmethod
    async def exists(
        self,
        frame_reference: str
    ) -> bool:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...