import os
import sys
import platform
import logging
import subprocess
import requests
import stat
import atexit
from miloco_server.config import RTSP_PORT, SERVER_CONFIG

logger = logging.getLogger(__name__)


class MediaMtxManager:
    def __init__(self):
        self.port = RTSP_PORT
        self.process = None
        self.bin_dir = os.path.join(os.getcwd(), "bin")
        self.bin_path = os.path.join(self.bin_dir, "mediamtx")
        if platform.system() == "Windows":
            self.bin_path += ".exe"

    def ensure_binary(self):
        if os.path.exists(self.bin_path): return

        logger.info("MediaMTX binary not found. Downloading...")
        if not os.path.exists(self.bin_dir): os.makedirs(self.bin_dir)

        system = platform.system().lower()
        machine = platform.machine().lower()
        version = "v1.9.1"

        if system == "darwin":
            os_name = "darwin"
            arch = "arm64" if "arm" in machine or "aarch64" in machine else "amd64"
        elif system == "linux":
            os_name = "linux"
            if "armv7" in machine:
                arch = "armv7"
            elif "aarch64" in machine or "arm64" in machine:
                arch = "arm64"
            else:
                arch = "amd64"
        else:
            logger.error(f"Unsupported OS: {system}")
            return

        url = f"https://github.com/bluenviron/mediamtx/releases/download/{version}/mediamtx_{version}_{os_name}_{arch}.tar.gz"

        try:
            import tarfile
            from io import BytesIO
            resp = requests.get(url)
            resp.raise_for_status()
            with tarfile.open(fileobj=BytesIO(resp.content), mode="r:gz") as tar:
                tar.extractall(path=self.bin_dir)

            st = os.stat(self.bin_path)
            os.chmod(self.bin_path, st.st_mode | stat.S_IEXEC)

            if system == "darwin":
                try:
                    subprocess.run(["xattr", "-d", "com.apple.quarantine", self.bin_path], stderr=subprocess.DEVNULL)
                except:
                    pass
        except Exception as e:
            logger.error(f"Failed to download MediaMTX: {e}")

    def create_hook_script(self, api_base):
        """生成钩子脚本，避免命令行转义噩梦"""
        script_path = os.path.join(self.bin_dir, "hook.sh")
        # 使用 $1 作为路径参数 (去除可能的前导斜杠)
        # 这里的逻辑是：接收信号时调用 stop，正常启动调用 start
        # 保持脚本运行直到收到信号
        script_content = f"""#!/bin/bash
API_BASE="{api_base}"
PATH_NAME=$MTX_PATH
QUERY_STR=$MTX_QUERY

# 去除可能的前导斜杠 (Fix for \\1053454049 issue)
PATH_NAME=${{PATH_NAME#/}}
PATH_NAME=${{PATH_NAME#\\\\}} 

# 定义停止函数
stop_stream() {{
    curl -k -sf "$API_BASE/stop/$PATH_NAME"
    exit 0
}}

# 捕获退出信号
trap stop_stream SIGTERM SIGINT

# 启动推流
curl -k -sf "$API_BASE/start/$PATH_NAME?$QUERY_STR"

# 挂起等待信号
while true; do sleep 1; done
"""
        with open(script_path, "w") as f:
            f.write(script_content)

        os.chmod(script_path, 0o755)
        return script_path

    def start(self):
        self.ensure_binary()
        if not os.path.exists(self.bin_path): return
        port = SERVER_CONFIG["port"]
        miloco_api_base = f"https://127.0.0.1:{port}/api/miot/internal/stream"

        # 创建脚本
        hook_script = self.create_hook_script(miloco_api_base)

        config_path = os.path.join(self.bin_dir, "mediamtx.yml")
        with open(config_path, "w") as f:
            f.write(f"""
paths:
  all:
    # 直接执行脚本，环境变量会自动传递
    runOnDemand: {hook_script}
    runOnDemandStartTimeout: 30s
    runOnDemandCloseAfter: 10s

rtspAddress: :{self.port}
rtmp: no
hls: no
webrtc: no
""")

        logger.info(f"Starting MediaMTX on port {self.port}...")
        self.process = subprocess.Popen(
            [self.bin_path, config_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        atexit.register(self.stop)

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None


rtsp_server = MediaMtxManager()