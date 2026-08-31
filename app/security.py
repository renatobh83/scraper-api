from fastapi import Security, HTTPException
from fastapi.security import APIKeyHeader
import secrets
from app.config import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)) -> None:
    if api_key is None or not secrets.compare_digest(api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="API key inválida ou ausente")
