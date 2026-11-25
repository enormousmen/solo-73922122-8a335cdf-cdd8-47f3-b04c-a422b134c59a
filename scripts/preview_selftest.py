#!/usr/bin/env python3
"""单格预览自测（仅标准库）。

覆盖需求点：
  1. 无任何已发布来源 => 预览地址 404（前端据此显示“暂无图”）；
  2. 未发布格 => 取最近已发布上级瓦片，裁剪框/放大倍数/原图坐标正确，输出 256x256 PNG；
  3. ETag + If-None-Match：来源坐标与原 PNG 内容不变 => 304；
  4. 坐标变化（即使来源字节完全相同）或内容变化 => ETag 变化、重新 200；
  5. 更近上级 / 正式瓦片发布后自动换来源；正式瓦片优先；
  6. 原 /tiles/*.png 地址仍只供正式瓦片（未发布 404）；
  7. 多个预览地址别名与 JSON 元数据地址可用且 ETag 一致；
  8. reset 与预览高并发相遇时无 5xx，200/304 响应头与图合法、可对应。

用法：先以同一 DATA_DIR 启动服务，再运行
  BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 scripts/preview_selftest.py
"""
from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
DATA = Path(os.environ.get("DATA_DIR", "./data"))
KIND = "regular"
T, X, Y, Z = 3, 7, 7, 3  # 目标格 z3/7/7


# ---------------------------------------------------------------- 基础工具

def request(method: str, url: str, headers: dict | None = None):
    req = urllib.request.Request(BASE + url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def api(method: str, path: str) -> dict:
    st, _, body = request(method, path)
    assert st < 400, f"{method} {path} -> {st} {body[:200]!r}"
    return json.loads(body)


def reset(kind: str = KIND) -> None:
    st, _, body = request("POST", f"/api/jobs/{kind}/reset")
    assert st == 200, f"reset {kind} -> {st} {body[:200]!r}"


def png_size(blob: bytes) -> tuple[int, int]:
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG"
    w, h = struct.unpack(">II", blob[16:24])
    return w, h


def write_png(path: Path, rgb=(200, 30, 30), size: int = 256) -> bytes:
    """写一张纯色 8-bit RGB PNG（标准库 zlib/struct）。"""
    path.parent.mkdir(parents=True, exist_ok=True)

    def chunk(tag: bytes, data: bytes) -> bytes:
        raw = tag + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(row * size, 6)) + chunk(b"IEND", b""))
    path.write_bytes(blob)
    return blob


def tile_file(z: int, x: int, y: int, kind: str = KIND) -> Path:
    return DATA / "tiles" / kind / str(z) / str(x) / f"{y}.png"


def preview_url(z=Z, x=X, y=Y, suffix=".png", kind=KIND) -> str:
    return f"/preview/{kind}/{z}/{x}/{y}{suffix}"


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✔ " + msg)


# ---------------------------------------------------------------- 场景

def test_empty_then_ancestor_and_304() -> str:
    print("== 测试1：暂无图 → z0 祖先裁剪 → ETag/304 ==")
    reset()
    st, hdr, _ = request("GET", preview_url())
    expect(st == 404, f"无来源时预览 404（实际 {st}）")

    write_png(tile_file(0, 0, 0), (210, 40, 40))
    st, hdr, body = request("GET", preview_url())
    expect(st == 200, f"有 z0 祖先后预览 200（实际 {st}）")
    expect(png_size(body) == (256, 256), "输出为 256×256 PNG")
    etag = hdr.get("etag")
    expect(bool(etag) and etag.startswith('"') and etag.endswith('"'), f"返回强 ETag：{etag}")
    low = hdr
    expect(low.get("x-source-type") == "ancestor", "X-Source-Type=ancestor")
    expect((low.get("x-source-z"), low.get("x-source-x"), low.get("x-source-y"))
           == ("0", "0", "0"), "来源坐标 0/0/0")
    expect(low.get("x-crop-box") == "224,224,256,256",
           f"z3/7/7 在 z0 瓦内裁剪框 224,224,256,256（实际 {low.get('x-crop-box')}）")
    expect(low.get("x-scale") == "8", "放大倍数 8×")
    expect(low.get("x-original-rect") == "1792,1792,2048,2048",
           f"换算回 2048 源图坐标 1792,1792,2048,2048（实际 {low.get('x-original-rect')}）")
    expect(low.get("cache-control") == "no-cache", "Cache-Control: no-cache（每次重验证）")

    st2, hdr2, body2 = request("GET", preview_url(), {"If-None-Match": etag})
    expect(st2 == 304, f"ETag 命中返回 304（实际 {st2}）")
    expect(body2 == b"", "304 无响应体（跳过裁剪产物）")
    expect(hdr2.get("etag") == etag, "304 带回同一 ETag")

    # 不带引号的 ETag 也应被容错识别
    st3, _, _ = request("GET", preview_url(), {"If-None-Match": etag.strip('"')})
    expect(st3 == 304, "裸 hex（无引号）If-None-Match 同样 304")
    print("  ✔ 测试1 通过\n")
    return etag


def test_closer_ancestor_and_official(old_etag: str) -> None:
    print("== 测试2：更近上级 / 正式瓦片发布后换来源、换 ETag ==")
    write_png(tile_file(2, 3, 3), (40, 70, 220))
    st, hdr, body = request("GET", preview_url(), {"If-None-Match": old_etag})
    expect(st == 200, f"更近 z2 发布后旧 ETag 不再命中 => 200（实际 {st}）")
    expect(png_size(body) == (256, 256), "新预览仍为 256×256")
    low = {k.lower(): v for k, v in hdr.items()}
    etag2 = low["etag"]
    expect(etag2 != old_etag, "ETag 已变化")
    expect((low.get("x-source-z"), low.get("x-source-x"), low.get("x-source-y"))
           == ("2", "3", "3"), "来源更新为最近上级 2/3/3")
    expect(low.get("x-crop-box") == "128,128,256,256", "z2 瓦内裁剪框 128,128,256,256")
    expect(low.get("x-scale") == "2", "放大倍数降为 2×")
    st, _, _ = request("GET", preview_url(), {"If-None-Match": etag2})
    expect(st == 304, "新 ETag 立即重验证 => 304")

    # 正式瓦片发布 => 最高优先级
    write_png(tile_file(Z, X, Y), (40, 200, 90))
    st, hdr, body = request("GET", preview_url(), {"If-None-Match": etag2})
    expect(st == 200, f"正式瓦片发布后 => 200（实际 {st}）")
    low = {k.lower(): v for k, v in hdr.items()}
    etag3 = low["etag"]
    expect(low.get("x-source-type") == "official", "X-Source-Type=official")
    expect((low.get("x-source-z"), low.get("x-source-x"), low.get("x-source-y"))
           == (str(Z), str(X), str(Y)), "来源即正式瓦片本身")
    expect(low.get("x-crop-box") == "0,0,256,256", "正式瓦片整格不裁")
    expect(low.get("x-scale") == "1", "scale=1")
    expect(etag3 not in (old_etag, etag2), "ETag 再次变化")

    # 同坐标覆盖内容 => ETag 必须随内容变化
    write_png(tile_file(Z, X, Y), (10, 10, 10))
    st, hdr, _ = request("GET", preview_url(), {"If-None-Match": etag3})
    expect(st == 200, f"同坐标内容变化 => 200（实际 {st}）")
    expect({k.lower(): v for k, v in hdr.items()}["etag"] != etag3, "内容变 => ETag 变")
    print("  ✔ 测试2 通过\n")


def test_same_bytes_different_coords() -> None:
    print("== 测试3：来源字节相同但坐标不同 => ETag 必须不同 ==")
    reset()
    same = write_png(tile_file(1, 0, 0), (123, 200, 50))
    tile_file(1, 1, 1).parent.mkdir(parents=True, exist_ok=True)
    tile_file(1, 1, 1).write_bytes(same)  # 完全相同的字节放到不同坐标

    st1, h1, _ = request("GET", preview_url(z=2, x=0, y=0))
    st2, h2, _ = request("GET", preview_url(z=2, x=3, y=3))
    expect(st1 == 200 and st2 == 200, "两个目标格都能预览")
    l1, l2 = {k.lower(): v for k, v in h1.items()}, {k.lower(): v for k, v in h2.items()}
    expect((l1.get("x-source-z"), l1.get("x-source-x"), l1.get("x-source-y")) == ("1", "0", "0"),
           "z2/0/0 的最佳上级是 1/0/0")
    expect((l2.get("x-source-z"), l2.get("x-source-x"), l2.get("x-source-y")) == ("1", "1", "1"),
           "z2/3/3 的最佳上级是 1/1/1")
    expect(l1["etag"] != l2["etag"], "字节相同但坐标不同 => ETag 不同")
    print("  ✔ 测试3 通过\n")


def test_tile_route_official_only_and_aliases() -> None:
    print("== 测试4：/tiles 只供正式瓦片；预览别名 / JSON 元数据 ==")
    # z3/0/0 从未发布（reset 后只在前面场景写过 z3/7/7，但测试3又 reset 了）
    st, _, _ = request("GET", "/tiles/regular/3/0/0.png")
    expect(st == 404, f"未发布正式瓦片 /tiles 仍 404（实际 {st}）")
    # 预览却可用：当前最佳来源是 z1/0/0（测试3）
    st, hdr, _ = request("GET", preview_url(z=2, x=0, y=0))
    expect(st == 200, "同一格 /preview 独立地址可取裁剪图")

    # 重新发布一张正式瓦片验证 /tiles 200
    write_png(tile_file(3, 0, 0), (1, 2, 3))
    st, _, body = request("GET", "/tiles/regular/3/0/0.png")
    expect(st == 200 and png_size(body) == (256, 256), "正式瓦片 /tiles 200 且为 256 PNG")

    paths = [
        "/preview/regular/3/0/0.png",
        "/preview/regular/3/0/0",
        "/preview/image/regular/3/0/0",
        "/tiles/regular/3/0/0/preview",
        "/api/preview/regular/3/0/0.png",
    ]
    etags = set()
    for p in paths:
        st, h, b = request("GET", p)
        expect(st == 200 and png_size(b) == (256, 256), f"别名 {p} => 200 256² PNG")
        etags.add({k.lower(): v for k, v in h.items()}["etag"])
    expect(len(etags) == 1, "所有图片别名共享同一 ETag")

    st, h, b = request("GET", "/api/preview/regular/3/0/0")
    expect(st == 200, "JSON 元数据地址 200")
    meta = json.loads(b)
    expect(meta["available"] is True and meta["width"] == 256 and meta["height"] == 256,
           "JSON 标记 available 且尺寸 256")
    expect(meta["source"]["type"] == "official" and meta["source_z"] == 3,
           "JSON 来源为正式瓦片 3/0/0")
    expect(bool(meta.get("png_base64")), "JSON 附带 png_base64")
    json_etag = {k.lower(): v for k, v in h.items()}["etag"]
    expect(json_etag == etags.pop(), "JSON ETag 与图片 ETag 一致")
    st, _, _ = request("GET", "/api/preview/regular/3/0/0", {"If-None-Match": json_etag})
    expect(st == 304, "JSON 地址同样支持 304")

    st, b = None, None
    st, _, b = request("GET", "/api/preview?kind=regular&z=3&x=0&y=0")
    expect(st == 200 and json.loads(b)["source_x"] == 0, "query 风格 /api/preview 可用")

    for bad in ["/preview/bogus/0/0/0.png", "/preview/regular/3/9/0.png",
                "/api/preview/nope/0/0/0"]:
        st, _, _ = request("GET", bad)
        expect(st == 404, f"非法参数 {bad} => 404（实际 {st}）")
    print("  ✔ 测试4 通过\n")


def test_reset_race() -> None:
    print("== 测试5：reset 与预览并发（不得 5xx；响应必须自洽）==")
    reset()
    write_png(tile_file(0, 0, 0), (9, 90, 160))
    stop = threading.Event()
    counts = {"200": 0, "304": 0, "404": 0, "other": 0}
    lock = threading.Lock()
    mismatches: list[str] = []

    def worker(i: int) -> None:
        known = None
        while not stop.is_set():
            headers = {"If-None-Match": known} if known else {}
            st, h, body = request("GET", preview_url(), headers)
            low = {k.lower(): v for k, v in h.items()}
            with lock:
                counts[str(st)] = counts.get(str(st), 0) + 1
            if st == 200:
                try:
                    if png_size(body) != (256, 256):
                        mismatches.append("200 尺寸非 256")
                    if not low.get("etag") or not low.get("x-source-z"):
                        mismatches.append("200 缺少 ETag/来源头")
                except AssertionError:
                    mismatches.append("200 body 不是合法 PNG")
                known = low.get("etag")
                # 立刻用本次 ETag 再验证一次：状态只可能是 304（未变）、
                # 200（期间又发布/换源）或 404（期间被 reset），绝不能 5xx。
                st2, h2, _ = request("GET", preview_url(), {"If-None-Match": known})
                if st2 not in (200, 304, 404):
                    mismatches.append(f"条件请求异常状态 {st2}")
                if st2 == 304 and ({k.lower(): v for k, v in h2.items()}.get("etag") != known):
                    mismatches.append("304 ETag 与请求的不一致")
            elif st == 304:
                if low.get("etag") != known:
                    mismatches.append("裸 304 的 ETag 不对应")
            elif st == 404:
                known = None
            elif st >= 500:
                mismatches.append(f"出现 5xx {st}")
            else:
                with lock:
                    counts["other"] += 1

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(worker, i) for i in range(12)]
        for _ in range(12):
            st, _, _ = request("POST", f"/api/jobs/{KIND}/reset")
            if st != 200:
                mismatches.append(f"并发 reset 状态 {st}")
            time.sleep(0.03)  # 留出“目录已空”的 404 窗口
            write_png(tile_file(0, 0, 0), (9, 90, 160))
            write_png(tile_file(3, 7, 7), (160, 90, 9))
            time.sleep(0.02)
        stop.set()
        for f in futs:
            f.result(timeout=20)

    expect(not mismatches, "并发期间所有响应自洽" + (f"（问题：{mismatches[:3]}）" if mismatches else ""))
    print(f"    并发状态计数：{counts}")
    expect(counts.get("200", 0) > 0, "压测中出现过 200")
    expect(counts.get("404", 0) > 0, "压测中出现过 reset 后的 404 窗口")
    print("  ✔ 测试5 通过\n")


def test_page_contains_preview() -> None:
    print("== 测试6：切片页包含预览 UI 与“暂无图” ==")
    st, _, html = request("GET", "/")
    expect(st == 200 and 'id="preview-modal"'.encode() in html, "首页含 #preview-modal")
    expect("暂无图".encode() in html, "HTML 内含“暂无图”占位")
    st, _, js = request("GET", "/app.js")
    text = js.decode("utf-8", "replace")
    expect(st == 200 and "If-None-Match" in text and "/preview/" in text,
           "app.js 使用独立 /preview/ 地址并带 If-None-Match 重验证")
    print("  ✔ 测试6 通过\n")


def main() -> int:
    print(f"BASE={BASE} DATA_DIR={DATA}\n")
    etag1 = test_empty_then_ancestor_and_304()
    test_closer_ancestor_and_official(etag1)
    test_same_bytes_different_coords()
    test_tile_route_official_only_and_aliases()
    test_reset_race()
    test_page_contains_preview()
    # 收尾：清掉自测夹具，恢复空闲干净状态
    request("POST", f"/api/jobs/{KIND}/reset")
    print("全部预览自测通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
