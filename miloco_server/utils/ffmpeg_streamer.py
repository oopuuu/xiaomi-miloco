import os
import logging
import subprocess
import threading
import queue
import time
from typing import Optional
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)

class PipeWriter(threading.Thread):
    """
    独立线程负责写入命名管道。
    [修复版] 实现了"漏桶策略" (Drop Oldest)，并增强了并发安全性。
    """
    def __init__(self, pipe_path, name):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        # 缓冲区大小：300 (约 1-2 秒数据，足够缓冲抖动，又能限制最大延迟)
        self.queue = queue.Queue(maxsize=300)
        self.fd = None
        self.running = True
        self._ensure_pipe()

    def _ensure_pipe(self):
        try:
            if os.path.exists(self.pipe_path):
                try: os.remove(self.pipe_path)
                except: pass
            os.mkfifo(self.pipe_path)
            # 使用 O_RDWR 防止 open 阻塞 (Linux/macOS)
            self.fd = os.open(self.pipe_path, os.O_RDWR)
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
        except Exception as e:
            logger.error(f"[{self.name}] Pipe error: {e}")
            self.running = False

    def write(self, data):
        if not self.running: return
        
        # [漏桶策略 - 健壮写法]
        # 尝试直接放入，如果满了，移除头部再放入
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            try:
                # 队列满，丢弃最老的数据 (Drop Oldest)
                self.queue.get_nowait()
                # 腾出空间后，再次尝试放入最新数据
                self.queue.put_nowait(data)
            except queue.Empty:
                # 极端情况：刚才满的瞬间被消费空了，直接放
                try: self.queue.put_nowait(data)
                except: pass
            except queue.Full:
                # 极端情况：刚腾出空间又满了，放弃本次写入
                pass
            except Exception:
                pass

    def run(self):
        while self.running:
            try:
                # 读出数据写入管道
                data = self.queue.get(timeout=1.0)
                if self.fd: os.write(self.fd, data)
            except queue.Empty:
                continue
            except OSError as e:
                # 管道破裂通常意味着 FFmpeg 退出了
                logger.error(f"[{self.name}] Pipe broken (FFmpeg stopped?): {e}")
                break
            except Exception as e:
                logger.error(f"[{self.name}] Unexpected error: {e}")
                break
        self.close()

    def close(self):
        self.running = False
        if self.fd:
            try: os.close(self.fd)
            except: pass
            self.fd = None
        if os.path.exists(self.pipe_path):
            try: os.remove(self.pipe_path)
            except: pass


class FFmpegStreamer:
    def __init__(self, camera_id: str, rtsp_target=None):
        self.camera_id = camera_id
        # 显式指定 localhost IP
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"
        
        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"
        
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None

    def start(self, video_codec="hevc"):
        self.stop()  # 先清理

        self.video_writer = PipeWriter(self.pipe_video, "Video")
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        self.audio_writer.start()

        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'info', '-hide_banner',
            
            # [时间戳控制]
            '-use_wallclock_as_timestamps', '1',
            '-fflags', '+genpts+nobuffer', 
            '-flags', 'low_delay',
            
            # [移除] -tune zerolatency (因为它只对编码有效，copy 模式下可能引发警告或错误)
            
            # [缓冲] 保持大缓冲以获取关键帧
            '-analyzeduration', '10000000', 
            '-probesize', '10000000',       

            # Video Input
            '-f', video_codec, '-use_wallclock_as_timestamps', '1', '-i', self.pipe_video,

            # Audio Input (G.711A 16k)
            '-f', 'alaw', '-ar', '16000', '-ac', '1', '-i', self.pipe_audio,

            '-map', '0:v', '-map', '1:a',

            # Video Output: Copy
            '-c:v', 'copy', '-bsf:v', 'hevc_mp4toannexb', 

            # Audio Output: Timestamp Fix + Opus
            '-af', 'aresample=16000,asetpts=N/SR/TB',
            '-c:a', 'libopus', '-b:a', '24k', '-ar', '16000', '-application', 'lowdelay',

            # Output
            '-f', 'rtsp', 
            '-rtsp_transport', 'tcp',
            '-max_muxing_queue_size', '400',
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg for {self.camera_id}...")
        self.process = subprocess.Popen(
            ffmpeg_cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.PIPE
        )
        
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            # 调试模式下多打一点日志，排查启动问题
            if "Error" in l or "rtsp" in l.lower() or "fps" in l:
                logger.info(f"[FFmpeg {self.camera_id}] {l}")

    def stop(self):
        if self.video_writer: self.video_writer.close()
        if self.audio_writer: self.audio_writer.close()
        
        if self.process:
            logger.info(f"Stopping FFmpeg for {self.camera_id}...")
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None

    def push_video(self, data):
        if self.video_writer: self.video_writer.write(data)

    def push_audio(self, data):
        if self.audio_writer: self.audio_writer.write(data)
