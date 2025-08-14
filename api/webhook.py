from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# Поймаем ЛЮБОЙ путь и ЛЮБОЙ метод внутри функции
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def catch_all(path: str, request: Request):
    return JSONResponse({"ok": True, "path": path, "method": request.method})
