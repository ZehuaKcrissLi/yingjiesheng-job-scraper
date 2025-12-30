# 应届生找工作爬虫 Yingjiesheng Job Scraper

本项目使用 Playwright（真实浏览器 UI）在“应届生求职/招聘”站点的搜索页面上进行翻页采集，并将结果保存为 JSONL 文件，便于后续用 Python/Excel/BI 工具分析。

## Disclaimer / 免责声明

使用本项目即表示你同意并理解以下条款：

- **用途限制**：本项目仅供学习、研究与个人使用。禁止用于商业用途、批量采集、绕过权限/付费机制、或任何可能对目标站点造成不合理负载/干扰的行为。
- **合规要求**：你必须遵守目标网站的服务条款、robots 协议（如适用）以及你所在地区的法律法规；由此产生的一切责任与后果由使用者自行承担。
- **风险提示**：自动化访问可能触发目标站点的风控策略，导致账号/IP 被限制或封禁；采集结果可能不完整或不准确。
- **隐私与凭证**：`yjs_state.json` 是 Playwright 登录态（可能包含敏感 cookie/token），**严禁上传/分享**。仓库默认通过 `.gitignore` 忽略该文件。
- **责任声明**：作者不对任何直接或间接损失（包括但不限于账号封禁、数据缺失、业务损失等）承担责任。

## 功能与边界

- **UI-only**：只通过浏览器 UI 翻页，不依赖私有接口签名；对页面遮挡/弹层做了专门处理。
- **需要登录态**：首次运行会要求你手动登录并保存 `yjs_state.json`（也可复用已有 state）。
- **输出**：按页与按岗位分别输出 JSONL（便于流式写入与增量追加）。

## 环境安装（uv）

在本目录（包含 `pyproject.toml`）执行：

```bash
uv sync
uv run playwright install chromium
```

说明：

- `uv sync` 会创建本地虚拟环境（默认 `.venv/`）并安装依赖。
- `playwright install chromium` 会下载浏览器内核（首次需要）。

## 使用方法

### 1) 准备登录态（首次或 state 失效时）

运行脚本后，如检测不到或无法使用 `yjs_state.json`，会打开浏览器窗口并提示你完成手动登录，然后保存 state。

### 2) 开始采集

在本目录下：

```bash
python main.py --area-name "山东" --keyword "人力资源"
```

常用参数：

- `--keyword`：搜索关键词（默认：`人力资源`）
- `--area-name`：地区名称（默认：`山东`，也可用 `全国` 或具体城市名，依赖 `dd_city.json`）
- `--state-path`：Playwright `storage_state` 路径（默认：`yjs_state.json`）
- `--max-page-actions`：最多翻页次数（默认：20）
- `--click-timeout-ms`：点击超时（默认：3000ms）
- `--no-progress-limit`：连续无进展阈值（默认：6）
- `--next-btn-selector` / `--next-btn-xpath`：Next 按钮定位（用于页面结构变化时自定义）

## 输出说明

默认会生成两类输出（文件名会包含关键词与地区）：

- `yingjiesheng_jobs_<关键词>_<地区>.jsonl`：逐岗位记录（已做字段扁平化，便于转 CSV）
- `yingjiesheng_pages_<关键词>_<地区>.jsonl`：逐页元信息（页号、条数、totalCount、requestId 等）

> 注意：仓库通过 `.gitignore` 默认忽略 `*.jsonl` 与 `*.csv`，避免误提交大文件/敏感数据。

## 排错指南（翻页/采集不动）

- **翻页点不到 / 一直不前进**：
  - 观察控制台日志中的 `hit_test` 输出（包含 `top/rect/center/viewport`）。
  - 发生超时时会在 `debug/` 下保存截图（若该目录存在/开启截图逻辑）。
  - 页面可能出现固定浮层/下拉弹层拦截点击；脚本会尝试 `Escape` 清场并对疑似拦截层禁用 `pointer-events`。
- **state 过期/无法获取第1页**：
  - 删除本地 `yjs_state.json` 后重新运行，按提示手动登录生成新 state。
- **area-name 报 “Ambiguous area name”**：
  - `dd_city.json` 可能存在同名城市的重复记录；脚本会对重复 code 自动去重。
  - 只有在去重后仍出现多个不同 code 时，才会提示真正歧义；此时请改用更精确的地区名称（例如带“省/市”后缀）或直接传入 `--area-name` 的完整匹配项。

## 目录结构建议

- `main.py`：主爬虫逻辑（Playwright UI + 翻页）
- `dd_city.json`：地区字典（用于 area-name -> jobarea code；可由脚本自动下载/更新）
- `yjs_state.json`：登录态（敏感，不提交）
- `debug/`：调试截图（不提交）
