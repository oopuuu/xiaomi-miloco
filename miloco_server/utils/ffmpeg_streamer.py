import os
import logging
import subprocess
import threading
import queue
import time
import re
from typing import Optional
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)


class PipeWriter(threading.Thread):
    """
    [漏桶策略 + 诊断监控] 独立线程写入管道，防止 FFmpeg 阻塞主线程
    """

    def __init__(self, pipe_path, name):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        # 加大缓冲池到 500 (约15-20秒)，给硬件编码器更多缓冲时间
        self.queue = queue.Queue(maxsize=500)
        self.fd = None
        self.running = True
        self._ensure_pipe()

        # [诊断] 流量统计
        self.total_bytes = 0
        self.last_log_time = time.time()
        self.drop_count = 0

    def _ensure_pipe(self):
        try:
            if os.path.exists(self.pipe_path):
                try:
                    os.remove(self.pipe_path)
                except:
                    pass
            os.mkfifo(self.pipe_path)
            self.fd = os.open(self.pipe_path, os.O_RDWR)
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
        except Exception as e:
            logger.error(f"[{self.name}] Pipe error: {e}")
            self.running = False

    def write(self, data):
        if not self.running: return

        # [诊断] 关键诊断点 1: 管道堵塞检测
        # 如果队列长期维持在高位 (>400)，说明下游(FFmpeg)处理太慢
        q_size = self.queue.qsize()
        if q_size > 400:
            if time.time() - self.last_log_time > 5:
                logger.warning(f"[{self.name}] ⚠️ BUFFER WARNING: Queue size {q_size}/500. FFmpeg is too slow!")
                self.last_log_time = time.time()

        # [漏桶逻辑] 队列满时，清空旧数据，强制追赶实时
        if self.queue.full():
            # [诊断] 丢包统计
            self.drop_count += 1
            if self.drop_count % 50 == 0:  # 每丢50个包报警一次
                logger.error(f"[{self.name}] 🚨 BUFFER FULL: Dropping packets! Total dropped: {self.drop_count}")
            
            try:
                with self.queue.mutex:
                    self.queue.queue.clear()
            except:
                pass

        try:
            self.queue.put_nowait(data)
            self.total_bytes += len(data)
        except:
            pass

    def run(self):
        while self.running:
            try:
                data = self.queue.get(timeout=1.0)
                if self.fd: os.write(self.fd, data)
            except queue.Empty:
                # [诊断] 如果长期没数据，可能是上游断流
                continue
            except OSError as e:
                logger.error(f"[{self.name}] OS Pipe Error: {e}")
                break
            except Exception as e:
                logger.error(f"[{self.name}] Unknown Error: {e}")
                break
        self.close()

    def close(self):
        self.running = False
        if self.fd:
            try:
                os.close(self.fd)
            except:
                pass
            self.fd = None
        if os.path.exists(self.pipe_path):
            try:
                os.remove(self.pipe_path)
            except:
                pass


class FFmpegStreamer:
    def __init__(self, camera_id: str, rtsp_target=None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"

        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"

        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None

        self._last_video_seq = -1
        
        # [诊断] 状态监控
        self._last_health_check = time.time()
        self._last_speed = 0.0

    def _get_video_output_args(self):
        """
        根据环境变量获取最佳的硬件加速参数
        ENV: MILOCO_HW_ACCEL = cpu | intel | nvidia | mac | rpi
        ENV: MILOCO_HW_DEVICE = /dev/dri/renderD128 (for intel/amd)
        """
        hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
        hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

        # 通用参数: 固定GOP为50(2秒), 禁用B帧(低延迟), 强制25fps
        common_opts = ['-g', '50', '-bf', '0', '-r', '25', '-profile:v', 'main']

        if hw_accel in ["intel", "amd", "vaapi"]:
            logger.info(f"Using HW Accel: VAAPI ({hw_device})")
            return [
                '-vaapi_device', hw_device,
                '-vf', 'format=nv12,hwupload,scale_vaapi=format=nv12',  # 修正：VAAPI缩放链
                '-c:v', 'h264_vaapi'
            ] + common_opts

        elif hw_accel in ["nvidia", "nvenc", "cuda"]:
            logger.info("Using HW Accel: NVIDIA NVENC")
            return [
                '-c:v', 'h264_nvenc',
                '-preset', 'p1',  # p1=fastest
                '-tune', 'zerolatency',  # 零延迟调优
                '-spatial-aq', '1'
            ] + common_opts

        elif hw_accel in ["mac", "apple", "videotoolbox"]:
            logger.info("Using HW Accel: Apple VideoToolbox")
            return [
                '-c:v', 'h264_videotoolbox',
                '-realtime', 'true',
                '-allow_sw', '1'
            ] + common_opts

        elif hw_accel in ["rpi", "raspberry", "v4l2m2m"]:
            logger.info("Using HW Accel: Raspberry Pi V4L2M2M")
            return [
                '-c:v', 'h264_v4l2m2m',
                '-num_capture_buffers', '16'
            ] + common_opts

        else:  # "cpu" or unknown -> Fallback to libx264
            logger.info("Using Software Encoding: libx264 (Ultrafast)")
            return [
                '-c:v', 'libx264',
                '-preset', 'ultrafast',  # 极速预设，CPU占用极低
                '-tune', 'zerolatency',  # 零延迟
            ] + common_opts

    def start(self, video_codec="hevc"):
        self.stop()
        self._last_video_seq = -1

        self.video_writer = PipeWriter(self.pipe_video, "Video")
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        self.audio_writer.start()

        # 动态获取视频编码参数
        video_out_args = self._get_video_output_args()

        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'info', '-hide_banner', # 改回info级别以便捕获统计信息

            # --- Global ---
            '-use_wallclock_as_timestamps', '1',
            '-fflags', '+genpts+nobuffer+igndts',
            '-flags', 'low_delay',
            '-err_detect', 'ignore_err',
            '-analyzeduration', '5000000',
            '-probesize', '5000000',

            # --- Video Input (Raw) ---
            '-f', video_codec, '-use_wallclock_as_timestamps', '1', '-i', self.pipe_video,

            # --- Audio Input (PCM s16le from internal decoder) ---
            # 必须匹配 decoder.py 输出的 16000Hz
            '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', self.pipe_audio,

            '-map', '0:v', '-map', '1:a',

            # --- Video Output (Dynamic HW/SW) ---
            *video_out_args,

            # --- Audio Output (AAC) ---
            # aresample=async=1000: 消除累积延迟的神器
            '-af', 'aresample=async=1000',
            '-c:a', 'aac', '-ar', '16000', '-b:a', '32k',

            # --- RTSP Output ---
            '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg (Diagnostic Mode) for {self.camera_id}...")
        self.process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        
        # [诊断] 正则表达式提取关键指标
        # 典型输出: frame= 123 fps= 25 q=28.0 size= 1024kB time=00:00:10.5 bitrate=123.4kbits/s speed=1.01x
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            
            # 1. 捕捉错误
            if "Error" in l or "fail" in l.lower() or "miss" in l.lower():
                # 过滤掉一些不影响运行的常见警告，只报重要的
                if "past duration" not in l and "non-monotonic" not in l:
                     logger.warning(f"[FFmpeg Warning] {l}")

            # 2. 捕捉性能指标 (每 30 秒打印一次，或性能异常时打印)
            if "frame=" in l:
                now = time.time()
                match = pattern.search(l)
                if match:
                    frame_cnt = int(match.group(1))
                    fps = float(match.group(2))
                    speed = float(match.group(3))
                    
                    self._last_speed = speed
                    
                    # 异常检测：如果处理速度低于 0.9x，说明编码器跟不上了，必然卡顿
                    if speed < 0.9:
                        if now - self._last_health_check > 5:
                            logger.warning(f"[FFmpeg Slow] 🐢 Speed: {speed}x (FPS: {fps}). GPU/CPU Overloaded!")
                            self._last_health_check = now
                    
                    # 正常心跳：每30秒输出一次，证明还在活
                    elif now - self._last_health_check > 30:
                        logger.info(f"[FFmpeg Health] ✅ Speed: {speed}x | FPS: {fps} | Frames: {frame_cnt}")
                        self._last_health_check = now
            else:
                # 启动时的关键信息
                if "Opening" in l or "Output" in l or "Input" in l or "Stream #" in l:
                    logger.info(f"[FFmpeg Info] {l}")

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

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        # 简单乱序过滤
        if self._last_video_seq != -1 and seq <= self._last_video_seq:
            if self._last_video_seq - seq < 10000: return
        self._last_video_seq = seq
        self.video_writer.write(data)

    def push_audio_raw(self, data: bytes):
        if self.audio_writer:
            self.audio_writer.write(data)
