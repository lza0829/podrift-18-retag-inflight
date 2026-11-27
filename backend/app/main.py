from __future__ import annotations
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import PROJECT_NAME
from app.routers import align, arm, images, orders, pods, policy, probes, quotas, settings
from app.seed import seed_if_empty
from app.services.errors import ApiError

seed_if_empty()

app = FastAPI(title=PROJECT_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(ApiError)
def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    # 业务错误统一形态：ApiError 已穿透 db_write 触发整笔回滚，这里只负责转 JSON
    body = {"错误": exc.message}
    if exc.conflict:
        body["冲突"] = True
    return JSONResponse(status_code=200, content=body)


for mod in (images, pods, orders, probes, quotas, policy, align, arm, settings):
    app.include_router(mod.router, prefix="/api")


@app.get("/api/健康")
def health_check():
    return {"产品": PROJECT_NAME, "状态": "正常"}
