"""应用配置。"""
import os


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# SQLite 数据库文件路径（容器内默认 /data/tide.db，由 compose 挂载卷）
DB_PATH = _env("DB_PATH", "/data/tide.db")

# 服务监听地址 / 端口
HOST = _env("HOST", "0.0.0.0")
PORT = int(_env("PORT", "8000"))

# 启动时若数据库为空，是否写入演示数据（节点/航段/潮位修订 #1）
SEED_DEMO = _env("SEED_DEMO", "true").lower() in ("1", "true", "yes")

# 规划窗口上限（分钟），题目固定 1440
MAX_WINDOW = int(_env("MAX_WINDOW", "1440"))

# 浮点比较容差（米）
EPS = 1e-9
