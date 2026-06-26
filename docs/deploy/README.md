# 部署

前端 build + nginx 反代 + systemd 管 uvicorn。

## 一次性

```bash
# 1. 后端
cd /opt/apex
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml   # 填入 TUSHARE_TOKEN / DEEPSEEK_API_KEY / BOCHA_API_KEY

# 2. 前端
cd frontend
npm ci
npm run build                        # 产物到 dist/
```

## systemd unit: uvicorn

`/etc/systemd/system/apex-backend.service`:

```ini
[Unit]
Description=apex FastAPI backend
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/apex
Environment="PATH=/opt/apex/.venv/bin"
ExecStart=/opt/apex/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now apex-backend
sudo journalctl -u apex-backend -f
```

> **workers=1**: SSE 流式(analyze/screener)需要常驻连接, 多 worker 会让 stream 中断。
> 单 worker 够用: 跑 4 年的个人交易记录, DeepSeek 调用是 IO 密集型不是 CPU 密集型。

## nginx

`/etc/nginx/sites-available/apex` (用 `docs/deploy/nginx.conf` 模板):

```bash
sudo cp docs/deploy/nginx.conf /etc/nginx/sites-available/apex
sudo ln -s /etc/nginx/sites-available/apex /etc/nginx/sites-enabled/apex
sudo nginx -t
sudo systemctl reload nginx
```

关键点:
- `proxy_buffering off` — SSE(analyze/screener run)必须关缓冲, 否则 trace 事件会积到 buffer 满了才一次性 flush
- `proxy_read_timeout 600s` — 单次 AI 分析可能跑 5-10 分钟
- `proxy_cache off` — 行情和 AI 流式都不缓存
- JS/CSS `immutable` 缓存 1y — vite 产物的文件名带 hash, 安全
- SPA 兜底 `try_files $uri /index.html` — react-router 的客户端路由

## 反向代理时 CORS

`backend/main.py` 的 CORS 配置默认 `allow_origins=["*"]`, 部署时收紧到
实际域名(防 CSRF 偷后端): 修改 `backend/core/lifespan.py` 或 `main.py`
的 CORSMiddleware 配置, 设成 `["https://apex.yourdomain.com"]` 之类。

## 前端环境变量

前端 dev 默认 `VITE_USE_MOCK=1`(无后端也能跑), 部署关掉:

```bash
# 在 build 之前
cd frontend
VITE_USE_MOCK=0 npm run build
```

> dev mock 是为了让 AI 评审时无后端也能看到红绿配色。
> 上线后必须 `VITE_USE_MOCK=0`, 否则所有数据都走前端 mock 不走后端。

## 验证

```bash
# 后端健康
curl -fsS http://localhost:8000/api/health

# 前端静态
curl -fsSI http://localhost/ | head -5

# 端到端: 走 nginx 命中后端
curl -fsS http://localhost/api/health
curl -fsS "http://localhost/api/market/index-daily?code=000001.SH" | head -c 200
```

## 升级

```bash
cd /opt/apex
git pull
source .venv/bin/activate
pip install -r requirements.txt   # 装了新包的话

cd frontend
npm ci
VITE_USE_MOCK=0 npm run build

sudo systemctl restart apex-backend
sudo systemctl reload nginx       # 静态资源由 nginx 直接服务, 不用 reload
```

## 数据备份

`apex/watchlist.py` + `apex/journal.py` 全在 `$HOME`:
- `~/.stock-watchlist/watchlist.json` — 自选股(active / candidate / archived)
- `~/.stock-journal/<ts_code>.jsonl` — 每只股票一份 JSONL, append-only

```bash
tar -czf apex-data-$(date +%Y%m%d).tar.gz \
  ~/.stock-watchlist ~/.stock-journal
```
