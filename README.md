# 切片值班台（常规底图 / 应急航片 · 应急插队）

FastAPI + Pillow 的切片演示服务：内置常规底图、应急航片各一张（2048×2048，启动时自动生成），
值班员可分别启动切片、查看进度、打开已发布的 PNG 瓦片；应急任务受理后立即抢占常规渲染。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements.txt
DATA_DIR=./data TILE_DELAY=0.15 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：`DATA_DIR`（源图/瓦片/日志目录，默认容器内 `/data`）、`TILE_DELAY`（每张瓦片模拟渲染耗时秒数，默认 0.15，调大可放慢演示节奏）。

## 页面演示（渲染到一半插队）

1. 点「启动切片」→ 常规底图进度条开始推进，瓦片逐张点亮；
2. 渲染到一半时点「启动应急切片（插队）」→ 常规立即变为「被抢占·待续算」，应急开始渲染；
3. 应急完成后常规自动续做（本轮计划数 = 剩余未发布张数，已发布的不重算）；
4. 勾选「模拟渲染失败」再启动应急，可演示应急失败后常规照常续做；
5. **点击任意格子（含尚未点亮的空格）打开「单格预览」**：优先显示正式瓦片；
   正式瓦片未发布时，自动取同一任务**最近的已发布上级瓦片**，按金字塔几何裁出对应区域并
   **最近邻放大到 256×256**。侧栏用文字 + 红框位置图标出**实际取图坐标**（来源瓦片 `z/x/y`、
   瓦内裁剪框、放大倍数、换算回 2048 源图的原图坐标）；没有任何已发布来源时显示「**暂无图**」。
   预览每 0.7s 自动重验证（带 `If-None-Match`），更近的上级或正式瓦片一发布，下一次重验证即换图并更新来源。
   已发布格可在弹窗里点「打开本格原 PNG」，也可用修饰键/中键点格子在新标签页打开 `/tiles/...` 原 PNG。
6. 「重置」清空该任务已发布瓦片（任务进行中返回 409，空闲/完成/失败态才允许）；重置后已打开的预览会在下一轮重验证变为「暂无图」。

## 实现内容

- **任务模型**（`app/scheduler.py`）：常规 / 应急两个任务，各 85 张瓦片（z0–z3，256px）。
  状态机：`idle → queued → running → done / failed / interrupted`；`interrupted` 的常规任务在应急结束后被调度器自动续做。
- **单渲染子进程**：调度主循环串行执行 `_run()`，同一时刻最多一个 `worker.py` 子进程；
  应急受理时若常规在跑，先中止旧进程并 `await proc.wait()`，确认退出后才启动应急进程。
- **抢占协议**：父进程向子进程 stdin 写 `ABORT` 指令（worker 用 `select` 监听 stdin，
  渲染睡眠可被立即唤醒，收到指令或 EOF 即以约定退出码 42 退出），同时补发 SIGTERM 双保险。
  不依赖信号即可工作（某些沙箱环境禁止 kill），正常 Linux / Docker 中两条通道都有效。
- **舍弃与重算**：瓦片先写 `.tmp` 再 `os.replace` 原子发布；被中止时当前瓦片尚未发布，
  残存 `.tmp` 由下次启动的 worker 清理，该瓦片在续做时重算。worker 跳过已存在的 PNG，
  已发布瓦片不重算；父进程每轮结束按磁盘重新对齐已发布集合。
- **应急失败钩子**：`POST /api/jobs/emergency/start?simulate_fail=true`，worker 发布 10 张后以退出码 3 模拟崩溃，用于自测与演示。
- **API**：
  - `GET /api/status` 两任务进度快照（状态、百分比、在渲瓦片、本轮计划、已发布清单）；
  - `POST /api/jobs/{kind}/start`（kind=regular|emergency，幂等；emergency 支持 `simulate_fail`）；
  - `POST /api/jobs/{kind}/reset`（任务进行中返回 409）；
  - `GET /tiles/{kind}/{z}/{x}/{y}.png`（**仅**提供已发布正式瓦片；未发布 404，不做裁剪顶替）；
  - `GET /preview/{kind}/{z}/{x}/{y}.png`（**单格预览独立地址**：正式瓦片优先，否则最近已发布上级瓦片
    裁剪放大到 256×256；无来源 404。响应给 `ETag` / `Cache-Control: no-cache` 与
    `X-Source-Type|Z|X|Y`、`X-Crop-Box`、`X-Scale`、`X-Original-Rect`、`X-Preview-Meta` 等来源头；
    请求带 `If-None-Match` 且「来源坐标 + 来源原 PNG 字节」未变时返回 **304** 并跳过裁剪）；
  - `GET /api/preview/{kind}/{z}/{x}/{y}`（同语义的 JSON 元数据：`available/source/crop/etag/image_url/png_base64`，
    同样支持 304；另有 `/api/preview?kind=&z=&x=&y=`、`/preview/...` 无 `.png` 后缀、
    `/tiles/.../preview`、`/api/tiles/.../preview` 等别名）；
  - `GET /sources/{kind}.png` 源图预览；`GET /` 演示页面。
- **预览一致性**（`app/preview.py`）：ETag = SHA-256(来源坐标标识 + 来源 PNG 字节)，坐标或内容变化都会换 ETag；
  解析来源时立即打开并读全文件字节，图、来源坐标、ETag 出自同一份数据，reset 删盘后本轮仍可返回已读入的图；
  另有 `IO_LOCK` 把 reset 与预览读盘/裁剪串行化，杜绝「图、来源、ETag 不对应」。
- **源图**（`app/sources.py`）：启动时用 Pillow 生成两张 2048×2048 区分度高的测试图（渐变 + 网格 + 坐标标注），已存在则不覆盖。

## 实测结果

环境：Python 3.11 / FastAPI 0.165 / Pillow 13，`TILE_DELAY=0.1`。自测脚本 `scripts/selftest.py`（仅标准库）：

```
== 测试A：应急插队 ==
  常规已发布 8 张，在渲 2/0/3，此刻受理应急…
  0.11s 内切换到应急，常规状态=interrupted
  应急完成 85/85，期间渲染进程始终为 emergency
  常规续做完成 85/85；8 张已发布瓦片 mtime 未变（未重算）；在渲瓦片 2/0/3 已重算；抽样瓦片均可取
  ✔ 测试A 通过
== 测试B：应急失败后续做 ==
  应急发布 10 张后失败（error='simulated render failure after 10 tiles'），等待常规续做…
  常规续做完成 85/85；应急失败留下的已发布瓦片仍可取
  ✔ 测试B 通过
```

另核验：

- 渲染期间 `ps` 实测 worker 进程数恒为 1；抢占后唯一进程即应急 worker（常规进程已退出）；
- `GET /`、`/app.js`、`/style.css`、`/sources/{kind}.png` 均 200；
- 已发布瓦片 200 且为合法 PNG，未发布瓦片 404；
- 复跑：`BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 scripts/selftest.py`。

单格预览自测 `scripts/preview_selftest.py`（仅标准库，内置纯色 PNG 夹具），6 组全部通过：

```
测试1 暂无图 404 → 发布 z0 后 200/256²，来源 0/0/0、裁剪 224,224,256,256、8×、原图坐标 1792,…,2048；
      If-None-Match 命中 => 304 空体；裸 ETag 也容错
测试2 z2/3/3 发布 => 旧 ETag 200 换源（裁剪 128,…、2×）；正式瓦片发布 => official/整格/1×；同坐标改内容 => ETag 变
测试3 两份完全相同字节放在不同坐标 => ETag 不同（坐标参与校验）
测试4 /tiles 未发布仍 404；5 个图片别名 + JSON 元数据全部 200 且 ETag 一致；JSON 也支持 304；非法参数 404
测试5 12 并发预览线程 × 主线程反复 reset/重发：无 5xx，200/304/404 均自洽（实测 200×156、404×269）
测试6 首页含 #preview-modal 与“暂无图”，app.js 使用 /preview/ 与 If-None-Match
```

另做了两层补充验证：

- **白盒**（Pillow）：逐像素核对最近邻放大结果等于「来源裁剪像素 ×N」；正式瓦片预览与原图像素一致；
  8 线程 `resolve/render` 与 reset 并发时，每个已返回来源的 ETag 都等于用本次 `data+坐标` 重算值；
  并专门验证「先解析读全、再 rmtree」后仍可渲染本次图像且三元组对应。
- **前端 DOM**（jsdom 对接真实运行服务，执行真实 `app.js`）：点格 → 弹窗拦截默认跳转 → 「暂无图」→
  发布 z0 后自动标出来源/裁剪框/原图坐标与位置红框 → 请求带 `If-None-Match` 并观察到界面 304 →
  z2 发布后自动换源 → 正式瓦片后切换 official 并出现 `/tiles/...` 原 PNG 链接 → 关闭。
- **真实切片联动**：全新数据目录启动 regular 切片，轮询 `z3/0/0` 预览，实测来源自然经历
  `404 → z0/0/0(32px,8×) → z1/0/0(64px,4×)+304 → z2/0/0(128px,2×)+304 → official z3/0/0`。
- 复跑：`BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 scripts/preview_selftest.py`。

## 未完成项 / 说明

- 本交付沙箱内**没有 docker / docker compose / podman，也没有守护进程与 `/var/run/docker.sock`**，
  因此 `docker compose up --build` 未能在本机实际构建；已用与 Dockerfile 完全相同的启动命令
  （`uvicorn app.main:app --host 0.0.0.0 --port 8000`，`DATA_DIR=./data`）在 :8000 实测页面与全部接口，
  `docker-compose.yml` 通过 YAML 解析校验，新增依赖为零（仅用标准库 + 已有的 FastAPI/Pillow）。
- 沙箱内也没有可用的无头 Chromium（缺 nss/atk/cups/gbm 等系统库且无 root），未做真实像素级浏览器截图；
  前端逻辑改用 jsdom 跑真实 `app.js` 验证，预览图本身已用 Pillow/图像查看核对裁剪内容。
- 调度状态在内存中，必须单 uvicorn worker 运行（Dockerfile 已固定单进程）；服务重启后已发布瓦片保留，
  但未完成任务需重新启动（不自动续跑）。
- 渲染耗时用 `TILE_DELAY` 模拟，未接真实切图负载；瓦片方案为单源图 z0–z3 固定金字塔，未做 XYZ 坐标系对齐与多比例尺重投影。
- 预览放大滤波器选用最近邻（取图像素与来源严格对应）；若希望平滑观感可改 LANCZOS，不影响坐标/ETag 语义。
- 未做鉴权与并发操作限流（重复启动幂等，重置仅做空态校验；预览/重置通过进程内锁串行化磁盘关键段）。
