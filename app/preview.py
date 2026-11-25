"""单格预览：正式瓦片优先，未发布时用同一任务最近的已发布上级瓦片裁剪放大。

设计要点（对应需求）：
- **最佳来源**：先找目标格的正式瓦片；没有就从 z-1 逐级向下找最近的已发布祖先瓦片，
  按金字塔几何裁出目标格对应的瓦内区域，放大到 256×256 PNG。
- **独立地址**：预览走 ``/preview/...``，原来的 ``/tiles/... .png`` 仍只提供正式瓦片，
  未发布一律 404，绝不拿裁剪图顶替。
- **ETag / If-None-Match**：ETag 是「来源坐标 + 来源原 PNG 字节」的哈希。
  两者都没变且 ``If-None-Match`` 命中时直接 304，不做裁剪/编码。
  一旦更近的上级或正式瓦片发布，来源坐标变化 => ETag 变化 => 200 换新图。
- **重置竞争**：解析来源后立即打开文件并把字节读全（Linux 下即使随后被 reset
  rmtree 删掉路径，已打开的 inode 仍可读完），图、来源坐标、ETag 全部来自这一次
  读到的同一份字节；另有 :data:`IO_LOCK` 把 reset 与预览读盘串行化，保证三元组对应。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from PIL import Image

from .scheduler import KINDS, TILE_SIZE

# 可复用的 If-None-Match 依赖类型（参数名仍显式声明，Header 自动转 if-none-match）。
INMHeader = Annotated[str | None, Header(alias="If-None-Match")]

PREVIEW_PX = 256
# 源图原始分辨率 2048 = 256 * 2**3，即 z3 与原 PNG 1:1；用于回标“原图坐标”。
NATIVE_ZOOM = 3
MAX_ZOOM = 10

# reset 与预览读图/裁剪共用的串行锁（reset 端点也会获取）。
IO_LOCK = threading.Lock()


# ---------------------------------------------------------------- 几何

def valid_cell(z: int, x: int, y: int) -> bool:
    return 0 <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def ancestor_geometry(z: int, x: int, y: int, sz: int) -> tuple[int, int, tuple[float, float, float, float], int]:
    """目标格 (z,x,y) 在上级瓦片 (sz) 中的瓦片坐标、瓦内裁剪框与放大倍数。

    第 z 级整幅边长 ``TILE_SIZE*2**z``；相差 d=z-sz 级时，上级一张瓦片覆盖
    下级 ``2**d`` 格，目标格在上级瓦内的裁剪边长为 ``TILE_SIZE/2**d``。
    """
    d = z - sz
    if d < 0:
        raise ValueError("source zoom must be <= target zoom")
    f = 2 ** d
    sx = x >> d
    sy = y >> d
    cell = TILE_SIZE / f
    l = (x - sx * f) * cell
    t = (y - sy * f) * cell
    return sx, sy, (l, t, l + cell, t + cell), f


def original_rect(sz: int, sx: int, sy: int, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """把上级瓦内裁剪框换算回 2048×2048 原始 PNG 的像素矩形。"""
    gx0, gy0 = sx * TILE_SIZE + box[0], sy * TILE_SIZE + box[1]
    gx1, gy1 = sx * TILE_SIZE + box[2], sy * TILE_SIZE + box[3]
    k = 2 ** (NATIVE_ZOOM - sz)
    return gx0 * k, gy0 * k, gx1 * k, gy1 * k


def fmt_num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def box_str(box: tuple[float, float, float, float]) -> str:
    return ",".join(fmt_num(v) for v in box)


# ---------------------------------------------------------------- 来源解析

@dataclass
class SourceRef:
    kind: str
    mode: str  # "official"（正式瓦片）| "ancestor"（已发布上级瓦片裁剪）
    z: int
    x: int
    y: int
    scale: int
    crop: tuple[float, float, float, float]
    orig_rect: tuple[float, float, float, float]
    data: bytes
    etag: str

    @property
    def coords(self) -> tuple[int, int, int]:
        return self.z, self.x, self.y

    def descriptor(self) -> dict[str, Any]:
        l, t, r, b = self.crop
        return {
            "type": self.mode,
            "kind": self.kind,
            "z": self.z,
            "x": self.x,
            "y": self.y,
            "tile": f"{self.z}/{self.x}/{self.y}",
            "tile_url": f"/tiles/{self.kind}/{self.z}/{self.x}/{self.y}.png",
            "crop": [round(l, 3), round(t, 3), round(r, 3), round(b, 3)],
            "crop_box": box_str(self.crop),
            "crop_width": round(r - l, 3),
            "crop_height": round(b - t, 3),
            "scale": self.scale,
            "original_rect": [round(v, 2) for v in self.orig_rect],
        }


def _try_open_tile(path: Path, identity: str) -> tuple[bytes, str] | None:
    """打开并读全一张候选瓦片。

    文件不存在（未发布 / 刚被 reset）返回 None；其它 OSError 也按“不可用”处理，
    不让预览接口冒出 5xx。ETag 只依赖来源坐标 identity 与文件字节。
    """
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        with f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return None
    h = hashlib.sha256()
    h.update(b"cell-preview/v1\0")
    h.update(identity.encode("ascii"))
    h.update(b"\0")
    h.update(data)
    etag = '"' + h.hexdigest() + '"'
    return data, etag


def resolve_source(tiles_dir: Path, kind: str, z: int, x: int, y: int) -> SourceRef | None:
    """按当前磁盘状态选最佳来源并一次性读入字节；没有任何可用图返回 None。"""
    base = Path(tiles_dir) / kind
    candidates: list[tuple[str, int, int, int]] = [("official", z, x, y)]
    for sz in range(z - 1, -1, -1):
        sx, sy, _, _ = ancestor_geometry(z, x, y, sz)
        candidates.append(("ancestor", sz, sx, sy))

    for mode, sz, sx, sy in candidates:
        path = base / str(sz) / str(sx) / f"{sy}.png"
        if mode == "official":
            crop = (0.0, 0.0, float(TILE_SIZE), float(TILE_SIZE))
            scale = 1
        else:
            _, _, crop, scale = ancestor_geometry(z, x, y, sz)
        identity = f"{kind}|{mode}|{sz}/{sx}/{sy}|{box_str(crop)}"
        opened = _try_open_tile(path, identity)
        if opened is None:
            continue
        data, etag = opened
        orect = original_rect(sz, sx, sy, crop)
        return SourceRef(
            kind=kind, mode=mode, z=sz, x=sx, y=sy, scale=scale,
            crop=crop, orig_rect=orect, data=data, etag=etag,
        )
    return None


def render_preview_png(ref: SourceRef, size: int = PREVIEW_PX) -> bytes:
    """从已读入的来源字节裁出目标区域并放大到 size×size，输出 PNG 字节。

    放大采用最近邻：值班员放大看格时每个输出像素都能对应回来源瓦像素，
    来源坐标标注与画面严格一致。
    """
    im = Image.open(io.BytesIO(ref.data))
    im.load()
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    crop_im = im.crop(tuple(ref.crop))
    up = crop_im.resize((size, size), Image.Resampling.NEAREST)
    out = io.BytesIO()
    up.save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------- 条件请求

def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    target = etag.strip('"')
    for raw in if_none_match.split(","):
        tok = raw.strip()
        if tok == "*":
            return True
        if tok[:2] in ("W/", "w/"):
            tok = tok[2:].strip()
        tok = tok.strip('"')
        if tok and tok == target:
            return True
    return False


def _check_cell(kind: str, z: int, x: int, y: int) -> None:
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="unknown job kind")
    if not valid_cell(z, x, y):
        raise HTTPException(status_code=404, detail="tile out of range")


def _source_headers(ref: SourceRef) -> dict[str, str]:
    d = ref.descriptor()
    l, t, r, b = ref.crop
    headers = {
        "ETag": ref.etag,
        "Cache-Control": "no-cache",
        "Access-Control-Expose-Headers": "*",
        "X-Preview-Available": "1",
        "X-Preview-Width": str(PREVIEW_PX),
        "X-Preview-Height": str(PREVIEW_PX),
        "X-Source-Type": ref.mode,
        "X-Source-Coordinates": f"{ref.z}/{ref.x}/{ref.y}",
        "X-Source-Kind": ref.kind,
        "X-Source-Z": str(ref.z),
        "X-Source-X": str(ref.x),
        "X-Source-Y": str(ref.y),
        "X-Source-Tile": f"{ref.kind}/{ref.z}/{ref.x}/{ref.y}.png",
        "X-Crop-Box": box_str(ref.crop),
        "X-Crop-X": fmt_num(l),
        "X-Crop-Y": fmt_num(t),
        "X-Crop-Width": fmt_num(r - l),
        "X-Crop-Height": fmt_num(b - t),
        "X-Scale": str(ref.scale),
        "X-Original-Rect": box_str(ref.orig_rect),
        "X-Preview-Meta": json.dumps(d, ensure_ascii=True, separators=(",", ":")),
    }
    return headers


def _empty_payload(kind: str, z: int, x: int, y: int) -> dict[str, Any]:
    return {
        "available": False,
        "kind": kind, "z": z, "x": x, "y": y,
        "width": PREVIEW_PX, "height": PREVIEW_PX,
        "reason": "no_published_source",
        "message": "暂无图：正式瓦片未发布，也没有已发布的上级瓦片",
        "image_url": f"/preview/{kind}/{z}/{x}/{y}.png",
    }


# ---------------------------------------------------------------- 路由

def build_router(data_dir: Path) -> APIRouter:
    router = APIRouter()
    tiles_dir = Path(data_dir) / "tiles"

    def both(path: str, **kwargs: Any):
        """注册 GET + HEAD。当前 FastAPI 版本不会给 GET 路由自动补 HEAD，
        显式带上后，值班/自测可用 HEAD 探 ETag 与来源头（uvicorn 对 HEAD 不写响应体）。"""
        kwargs["methods"] = ["GET", "HEAD"]
        return router.api_route(path, **kwargs)

    def image_core(kind: str, z: int, x: int, y: int, inm: str | None) -> Response:
        _check_cell(kind, z, x, y)
        # 与 reset 串行；resolve_source 内部也已“打开并读全”，双保险。
        with IO_LOCK:
            ref = resolve_source(tiles_dir, kind, z, x, y)
            if ref is None:
                raise HTTPException(status_code=404, detail=_empty_payload(kind, z, x, y))
            headers = _source_headers(ref)
            if etag_matches(inm, ref.etag):
                # 来源坐标 + 原 PNG 内容未变且 ETag 命中：跳过裁剪/编码。
                return Response(status_code=304, headers=headers)
            png = render_preview_png(ref)
        headers["Content-Length"] = str(len(png))
        return Response(content=png, media_type="image/png", headers=headers)

    def json_core(kind: str, z: int, x: int, y: int, inm: str | None) -> Response:
        _check_cell(kind, z, x, y)
        with IO_LOCK:
            ref = resolve_source(tiles_dir, kind, z, x, y)
            if ref is None:
                return JSONResponse(status_code=404, content=_empty_payload(kind, z, x, y),
                                   headers={"Cache-Control": "no-cache"})
            headers = _source_headers(ref)
            if etag_matches(inm, ref.etag):
                return Response(status_code=304, headers=headers)
            png = render_preview_png(ref)
            d = ref.descriptor()
            payload: dict[str, Any] = {
                "available": True,
                "kind": kind, "z": z, "x": x, "y": y,
                "width": PREVIEW_PX, "height": PREVIEW_PX, "size": PREVIEW_PX,
                "etag": ref.etag,
                "cache_control": "no-cache",
                "image_url": f"/preview/{kind}/{z}/{x}/{y}.png",
                "preview_url": f"/preview/{kind}/{z}/{x}/{y}.png",
                "url": f"/preview/{kind}/{z}/{x}/{y}.png",
                "source": d,
                # 平铺一份，方便直接取字段
                "source_type": ref.mode,
                "source_kind": ref.kind,
                "source_z": ref.z, "source_x": ref.x, "source_y": ref.y,
                "source_tile": d["tile"],
                "source_tile_url": d["tile_url"],
                "crop": d["crop"],
                "crop_box": d["crop_box"],
                "scale": ref.scale,
                "original_rect": d["original_rect"],
                "png_base64": base64.b64encode(png).decode("ascii"),
            }
        return JSONResponse(content=payload, headers=headers)

    # ---- 图片地址（规范地址：/preview/{kind}/{z}/{x}/{y}.png）----
    # 注意：带 .png 后缀的路由必须先于无后缀路由注册，否则 y 会被当成 "0.png"。
    @both("/preview/{kind}/{z}/{x}/{y}.png", include_in_schema=False)
    def preview_png_suffix(kind: str, z: int, x: int, y: int,
                           if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    @both("/preview/{kind}/{z}/{x}/{y}", name="cell_preview_png")
    def preview_png(kind: str, z: int, x: int, y: int,
                    if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    @both("/preview/image/{kind}/{z}/{x}/{y}.png", include_in_schema=False)
    def preview_image_suffix(kind: str, z: int, x: int, y: int,
                            if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    @both("/preview/image/{kind}/{z}/{x}/{y}", include_in_schema=False)
    def preview_image(kind: str, z: int, x: int, y: int,
                      if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    # 子资源风格 /tiles/{kind}/z/x/y/preview（原 .png 地址语义不变）
    @both("/tiles/{kind}/{z}/{x}/{y}/preview", include_in_schema=False)
    def tile_subresource_preview(kind: str, z: int, x: int, y: int,
                                 if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    @both("/api/tiles/{kind}/{z}/{x}/{y}/preview", include_in_schema=False)
    def api_tile_subresource_preview(kind: str, z: int, x: int, y: int,
                                     if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    # query 风格图片地址 /preview/image?kind=&z=&x=&y=
    @both("/preview/image", include_in_schema=False)
    def preview_image_query(kind: str = Query(...), z: int = Query(...),
                            x: int = Query(...), y: int = Query(...),
                            if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    # ---- JSON 元数据地址 ----
    @both("/api/preview/meta/{kind}/{z}/{x}/{y}", include_in_schema=False)
    def preview_meta(kind: str, z: int, x: int, y: int,
                     if_none_match: INMHeader = None):
        return json_core(kind, z, x, y, if_none_match)

    @both("/api/preview/{kind}/{z}/{x}/{y}.png", include_in_schema=False)
    def api_preview_png(kind: str, z: int, x: int, y: int,
                        if_none_match: INMHeader = None):
        return image_core(kind, z, x, y, if_none_match)

    @both("/api/preview/{kind}/{z}/{x}/{y}", name="cell_preview_meta")
    def api_preview_meta(kind: str, z: int, x: int, y: int,
                         if_none_match: INMHeader = None):
        return json_core(kind, z, x, y, if_none_match)

    @both("/api/preview", include_in_schema=False)
    def api_preview_query(kind: str = Query(...), z: int = Query(...),
                          x: int = Query(...), y: int = Query(...),
                          if_none_match: INMHeader = None):
        return json_core(kind, z, x, y, if_none_match)

    return router
