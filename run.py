"""Entry point: python run.py"""
import uvicorn

from backend.config import settings

if __name__ == "__main__":
    print(f"\n  Infinite Flight Tracker -> http://{settings.host}:{settings.port}")
    print(f"  mode: {'MOCK (sample data)' if settings.mock else 'LIVE'}\n")
    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=False)
