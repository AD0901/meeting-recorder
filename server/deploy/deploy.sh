#!/usr/bin/env bash
# 把服务端部署 / 更新到云服务器（Ubuntu 22.04 / 24.04）。在 Mac 上运行：
#
#     server/deploy/deploy.sh <服务器公网IP>
#
# 第一次运行会：建 meetrec 用户、装 Python、建虚拟环境、装 systemd 服务。
# 以后改了代码再运行一次就是更新，data/ 里的录音和纪要不会动。
#
# 云上用的配置是 server/.env.cloud（不进 git）：第一次运行时从 server/.env 复制，
# 并自动生成设备口令 API_TOKEN 和网页密码 WEB_PASSWORD。
set -euo pipefail
HOST="${1:?用法: deploy.sh <服务器IP>}"
KEY="${SSH_KEY:-$HOME/.ssh/meetrec_deploy}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "root@$HOST")
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ENV_CLOUD="$HERE/.env.cloud"

rand() { LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c "$1"; }

if [ ! -f "$ENV_CLOUD" ]; then
  grep -vE '^(AUTO_OPEN|PUBLIC_URL|API_TOKEN|WEB_USER|WEB_PASSWORD)=' "$HERE/.env" > "$ENV_CLOUD"
  {
    echo
    echo "# ---- 云服务器专用（deploy.sh 生成） ----"
    echo "AUTO_OPEN=0"
    echo "PUBLIC_URL=http://$HOST:8000"
    echo "API_TOKEN=$(rand 12)"
    echo "WEB_USER=admin"
    echo "WEB_PASSWORD=$(rand 12)"
  } >> "$ENV_CLOUD"
  chmod 600 "$ENV_CLOUD"
  echo "已生成 $ENV_CLOUD（设备口令和网页密码都在里面）"
fi

echo "① 准备服务器环境"
"${SSH[@]}" bash -s <<'REMOTE'
set -e
id meetrec >/dev/null 2>&1 || useradd -r -m -d /opt/meeting-recorder -s /usr/sbin/nologin meetrec
if ! command -v python3 >/dev/null || ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv rsync >/dev/null
fi
command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync >/dev/null)
mkdir -p /opt/meeting-recorder/server/data
REMOTE

echo "② 上传代码和配置"
rsync -az --delete -e "ssh -i $KEY" \
  --exclude .venv --exclude data --exclude .env --exclude .env.cloud --exclude __pycache__ \
  "$HERE/" "root@$HOST:/opt/meeting-recorder/server/"
scp -q -i "$KEY" "$ENV_CLOUD" "root@$HOST:/opt/meeting-recorder/server/.env"

echo "③ 安装依赖并启动服务"
"${SSH[@]}" bash -s <<'REMOTE'
set -e
cd /opt/meeting-recorder/server
[ -x .venv/bin/python ] || python3 -m venv .venv
# 阿里云 ECS 内网镜像，不走公网流量
.venv/bin/pip install -q -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com \
  -r requirements.txt 2>/dev/null || .venv/bin/pip install -q -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
chown -R meetrec:meetrec /opt/meeting-recorder
chmod 600 .env
cp deploy/meeting-recorder.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q meeting-recorder
systemctl restart meeting-recorder
sleep 2
systemctl is-active meeting-recorder
REMOTE

TOKEN=$(grep '^API_TOKEN=' "$ENV_CLOUD" | cut -d= -f2)
if curl -fsS -m 5 -H "Authorization: Bearer $TOKEN" "http://$HOST:8000/api/health" >/dev/null; then
  echo "✅ 部署完成：http://$HOST:8000"
else
  echo "⚠️  服务已启动，但从这里访问不到 http://$HOST:8000 —— 检查云服务器防火墙是否放行了 TCP 8000"
fi
