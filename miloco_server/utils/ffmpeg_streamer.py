import os
import logging
import subprocess
import threading
import queue
import time
import heapq
import re
from typing import Optional, List, Tuple
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)

class VideoJitterBuffer:
    """
    [核心修复] 视频抖动缓冲区
    用于解决 P2P 网络导致的乱序(Out-of-Order)和抖动(Jitter)问题。
    它会缓存一定数量的帧，按 SEQ 排序后再吐出，确保 FFmpeg 收到的是线性时间轴。
    """
    def __init__(self, max_latency_frames=25):
        self.buffer: List[Tuple[int, bytes]] = [] # Min-Heap: (seq, data)
        self.next_seq = -1
        self.max_latency = max_latency_frames # 最大缓冲帧数 (25帧约等于1秒延迟)
        self.force_reset_threshold = 1000 # 如果序号跳跃太大，强制重置

    def push(self, data: bytes, seq: int) -> List[bytes]:
        """
        存入一帧，并返回目前可以“安全释放”的有序帧列表
        """
        output_frames = []

        # 1. 初始化
        if self.next_seq == -1:
            self.next_seq = seq
        
        # 2. 异常重置：如果收到序号与期望序号差距过大（如摄像头重启或严重丢包）
        # 或者收到旧包太久远
        if abs(seq - self.next_seq) > self.force_reset_threshold:
            logger.warning(f"[JitterBuffer] Seq jump detected (Exp: {self.next_seq}, Got: {seq}). Resetting buffer.")
            self.buffer = []
            self.next_seq = seq
        
        # 3. 如果收到的是旧包（已经处理过的），直接丢弃
        if seq < self.next_seq:
            # logger.debug(f"Drop late packet: {seq} < {self.next_seq}")
            return []

        # 4. 入堆 (自动排序)
        heapq.heappush(self.buffer, (seq, data))

        # 5. 尝试提取连续帧
        # 只要堆顶的序号 == 我们期望的序号，就立刻吐出
        while self.buffer and self.buffer[0][0] == self.next_seq:
            s, d = heapq.heappop(self.buffer)
            output_frames.append(d)
            self.next_seq += 1

        # 6. 强制输出（防死锁策略）
        # 如果缓冲区积压太多（说明中间缺了一帧，一直没等到），只能忍痛跳过那个缺的帧
        if len(self.buffer) > self.max_latency:
            # 丢弃期望的帧(因为它一直没来)，直接跳到堆顶的帧
            lost_seq = self.next_seq
            new_seq, d = heapq.heappop(self.buffer)
            output_frames.append(d)
            
            # logger.warning(f"[JitterBuffer] Packet Loss! Skipped {lost_seq} -> {new_seq} to catch up.")
            self.next_seq = new_seq + 1
            
            # 继续尝试吐出后续连续的帧
            while self.buffer and self.buffer[0][0] == self.next_seq:
                s, d = heapq.heappop(self.buffer)
                output_frames.append(d)
                self.next_seq += 1
                
        return output_frames

class PipeWriter(threading.Thread):
    def __init__(self, pipe_path, name):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        self.queue = queue.Queue(maxsize=500)
        self.fd = None
        self.running = True
        self._ensure_pipe()
        self.drop_count = 0
        self.last_log_time = time.time()

    def _ensure_pipe(self):
        try:
            if os.path.exists(self.pipe_path):
                try: os.remove(self.pipe_path)
                except: pass
            os.mkfifo(self.pipe_path)
            self.fd = os.open(self.pipe_path, os.O_RDWR)
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
        except Exception as e:
            logger.error(f"[{self.name}] Pipe error: {e}")
            self.running = False

    def write(self, data):
        if not self.running: return
        
        # [诊断] 管道监控
        q_size = self.queue.qsize()
        if q_size > 450:
             if time.time() - self.last_log_time > 10:
                logger.warning(f"[{self.name}] ⚠️ Pipe Congestion: {q_size}/500. FFmpeg encoding is slow.")
                self.last_log_time = time.time()

        if self.queue.full():
            self.drop_count += 1
            try:
                with self.queue.mutex: self.queue.queue.clear()
            except: pass
            if self.drop_count % 50 == 0:
                logger.error(f"[{self.name}] 🚨 Pipe Full! Dropped {self.drop_count} packets.")
        
        try:
            self.queue.put_nowait(data)
        except: pass

    def run(self):
        while self.running:
            try:
                data = self.queue.get(timeout=1.0)
                if self.fd: os.write(self.fd, data)
            except queue.Empty: continue
            except OSError: break
            except: break
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
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"
        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None
        
        # [核心] 引入 Jitter Buffer
        self.jitter_buffer = VideoJitterBuffer(max_latency_frames=30)
        
        # 诊断变量
        self._last_health_check = time.time()

    def _get_video_output_args(self):
        hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
        hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")
        
        # 关键修改：去除 -r 25 强制帧率，允许动态帧率以适应网络波动
        # 增加 vsync vfr (变量帧率)
        common_opts = ['-g', '50', '-bf', '0', '-fps_mode', 'vfr'] 

        if hw_accel in ["intel", "amd", "vaapi"]:
            logger.info(f"Using HW Accel: VAAPI ({hw_device})")
            return ['-vaapi_device', hw_device, '-vf', 'format=nv12,hwupload,scale_vaapi=format=nv12', '-c:v', 'h264_vaapi'] + common_opts
        elif hw_accel in ["nvidia", "nvenc", "cuda"]:
            logger.info("Using HW Accel: NVIDIA NVENC")
            return ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'zerolatency'] + common_opts
        elif hw_accel in ["mac", "apple", "videotoolbox"]:
            logger.info("Using HW Accel: Apple VideoToolbox")
            return ['-c:v', 'h264_videotoolbox', '-realtime', 'true'] + common_opts
        elif hw_accel in ["rpi", "raspberry"]:
            logger.info("Using HW Accel: RPi")
            return ['-c:v', 'h264_v4l2m2m'] + common_opts
        else:
            logger.info("Using SW Encoding: libx264")
            return ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency'] + common_opts

    def start(self, video_codec="hevc"):
        self.stop()
        self.jitter_buffer = VideoJitterBuffer(max_latency_frames=30) # 重置 Buffer

        self.video_writer = PipeWriter(self.pipe_video, "Video")
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        self.audio_writer.start()

        video_out_args = self._get_video_output_args()

        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'info', '-hide_banner',

            # --- Input Options ---
            # 关键：移除 wallclock 强制时间戳，因为现在我们有 JitterBuffer 整理顺序了
            # 让 FFmpeg 自己处理 PTS 会更平滑
            '-fflags', '+genpts+nobuffer+igndts',
            '-flags', 'low_delay',
            '-analyzeduration', '2000000',
            '-probesize', '2000000',

            # --- Video Input ---
            '-f', video_codec, 
            # 尝试指定输入帧率，帮助 FFmpeg 稳定时间轴
            '-r', '20', 
            '-i', self.pipe_video,

            # --- Audio Input ---
            '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', self.pipe_audio,

            '-map', '0:v', '-map', '1:a',

            # --- Video Output ---
            *video_out_args,

            # --- Audio Output ---
            '-af', 'aresample=async=1000',
            '-c:a', 'aac', '-ar', '16000', '-b:a', '32k',

            # --- RTSP Output ---
            '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg for {self.camera_id}...")
        self.process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            
            # 诊断逻辑
            if "frame=" in l:
                now = time.time()
                match = pattern.search(l)
                if match and (now - self._last_health_check > 30):
                    logger.info(f"[FFmpeg Status] {l}")
                    self._last_health_check = now
            elif "Error" in l or "fail" in l.lower():
                if "past duration" not in l: logger.warning(f"[FFmpeg Warning] {l}")

    def stop(self):
        if self.video_writer: self.video_writer.close()
        if self.audio_writer: self.audio_writer.close()
        if self.process:
            self.process.terminate()
            try: self.process.wait(timeout=2)
            except: self.process.kill()
            self.process = None

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        # [核心变更] 不再直接 write，而是通过 JitterBuffer 排序
        # 这会自动处理乱序帧，并按正确顺序返回数据
        ordered_frames = self.jitter_buffer.push(data, seq)
        
        for frame_data in ordered_frames:
            self.video_writer.write(frame_data)

    def push_audio_raw(self, data: bytes):
        if self.audio_writer:
            self.audio_writer.write(data)
