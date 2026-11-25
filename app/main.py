"""FastAPI 入口：切片值班台。

接口：
  GET  /api/status                     两个任务的进度快照
  POST /api/jobs/{kind}/start          启动切片（kind=regular|emergency；emergency 支持 ?simulate_fail=true 自测失败）
  POST /api/jobs/{kind}/reset          清空已发布瓦片（任务空闲时才允许）
  GET  /tiles/{kind}/{z}/{x}/{y}.png   取已发布正式瓦片（未发布 404；不提供裁剪/预览）
  GET  /preview/{kind}/{z}/{x}/{y}.png 单格预览图（正式瓦片优先，否则最近已发布上级瓦片裁剪放大到 256×256；
                                       支持 ETag / If-None-Match，来源不变返回 304）
  GET  /api/preview/{kind}/{z}/{x}/{y} 单格预览 JSON 元数据（来源坐标、裁剪框、ETag 等，同样支持 304）
  GET  /sources/{kind}.png             源图预览
  GET  /                               演示页面
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .preview import IO_LOCK, build_router
from .scheduler import KINDS, Scheduler
from .sources import ensure_sources

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
TILE_DELAY = float(os.environ.get("TILE_DELAY", "0.15"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

sched = Scheduler(DATA_DIR, TILE_DELAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_sources(DATA_DIR)
    sched.start()
    yield
    await sched.shutdown()


app = FastAPI(title="应急切片插队服务", lifespan=lifespan)


def _check(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(404, "unknown job kind")


@app.get("/api/status")
async def status():
    return sched.snapshot()


@app.post("/api/jobs/{kind}/start")
async def start(kind: str, simulate_fail: bool = False):
    _check(kind)
    job = sched.request_start(kind, simulate_fail=simulate_fail)
    return {"ok": True, "job": job.snapshot()}


@app.post("/api/jobs/{kind}/reset")
def reset(kind: str):
    # 普通 def => 在线程池执行；与预览读图共用 IO_LOCK，
    # 使“重置删盘”和“解析来源/读图/裁剪”不会交叉产生不一致的三元组。
    _check(kind)
    with IO_LOCK:
        try:
            job = sched.reset(kind)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
    return {"ok": True, "job": job.snapshot()}


@app.api_route("/tiles/{kind}/{z}/{x}/{y}.png", methods=["GET", "HEAD"])
def tile(kind: str, z: int, x: int, y: int):
    _check(kind)
    if z < 0 or z > 10 or x < 0 or y < 0 or x >= 2 ** z or y >= 2 ** z:
        raise HTTPException(404, "tile out of range")
    p = DATA_DIR / "tiles" / kind / str(z) / str(x) / f"{y}.png"
    if not p.is_file():
        raise HTTPException(404, "tile not published")
    return FileResponse(p, media_type="image/png")


@app.get("/sources/{kind}.png")
def source(kind: str):
    _check(kind)
    p = DATA_DIR / "sources" / f"{kind}.png"
    if not p.is_file():
        raise HTTPException(404, "source missing")
    return FileResponse(p, media_type="image/png")


# 单格预览的独立地址（图片 + JSON 元数据 + 若干兼容别名），必须在静态兜底挂载之前注册。
app.include_router(build_router(DATA_DIR))


# 静态页面挂在最后，避免覆盖上面的 API 路由
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
