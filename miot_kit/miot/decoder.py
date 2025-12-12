# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT Decoder.
"""
import asyncio
from collections import deque
import logging
import subprocess
import threading
import time
import audioop
from typing import List, Callable, Coroutine, Optional
from io import BytesIO
from av.packet import Packet
from av.codec import CodecContext
from av.video.codeccontext import VideoCodecContext
from av.audio.codeccontext import AudioCodecContext
from av.audio.resampler import AudioResampler
from av.video.frame import VideoFrame
from av.audio.frame import AudioFrame
from PIL import Image

from .types import MIoTCameraFrameType, MIoTCameraCodec, MIoTCameraFrameData
from .error import MIoTMediaDecoderError

_LOGGER = logging.getLogger(__name__)


class MIoTMediaRingBuffer():
    """Ring buffer."""
    _maxlen: int
    _video_buffer: deque[MIoTCameraFrameData]
    _audio_buffer: deque[MIoTCameraFrameData]
    _cond: threading.Condition

    def __init__(self, maxlen: int = 20):
        self._maxlen = maxlen
        self._video_buffer = deque(maxlen=maxlen)
        self._audio_buffer = deque(maxlen=maxlen)
        self._cond = threading.Condition()

    def put_video(self, item: MIoTCameraFrameData) -> None:
        with self._cond:
            if len(self._video_buffer) >= self._maxlen:
                if item.frame_type == MIoTCameraFrameType.FRAME_I:
                    removed: bool = False
                    for i in range(len(self._video_buffer)):
                        if self._video_buffer[i].frame_type != MIoTCameraFrameType.FRAME_I:
                            del self._video_buffer[i]
                            removed = True
                            break
                    if not removed:
                        self._video_buffer.popleft()
                    self._video_buffer.append(item)
                    self._cond.notify()
                else:
                    pass
            else:
                self._video_buffer.append(item)
                self._cond.notify()

    def put_audio(self, item: MIoTCameraFrameData) -> None:
        with self._cond:
            self._audio_buffer.append(item)
            self._cond.notify()

    def step(
            self,
            on_video_frame: Callable[[MIoTCameraFrameData], None],
            on_audio_frame: Callable[[MIoTCameraFrameData], None],
            timeout: float = 0.2
    ) -> None:
        video_data: Optional[MIoTCameraFrameData] = None
        audio_data: Optional[MIoTCameraFrameData] = None

        with self._cond:
            if self._audio_buffer:
                audio_data = self._audio_buffer.popleft()
            elif self._video_buffer:
                video_data = self._video_buffer.popleft()
            else:
                self._cond.wait(timeout=timeout)
                if self._audio_buffer:
                    audio_data = self._audio_buffer.popleft()
                elif self._video_buffer:
                    video_data = self._video_buffer.popleft()

        if audio_data:
            on_audio_frame(audio_data)
        elif video_data:
            on_video_frame(video_data)

    def stop(self):
        with self._cond:
            self._cond.notify_all()
        self._video_buffer.clear()
        self._audio_buffer.clear()


class MIoTMediaDecoder(threading.Thread):
    """MIoT Decoder."""
    _main_loop: asyncio.AbstractEventLoop
    _running: bool
    _frame_interval: int
    _enable_hw_accel: bool
    _enable_audio: bool

    _video_callback: Callable[[bytes, int, int], Coroutine]
    _audio_callback: Callable[[bytes, int, int], Coroutine]

    _queue: MIoTMediaRingBuffer
    _video_decoder: Optional[CodecContext]
    _audio_decoder: Optional[CodecContext]
    _resampler: AudioResampler

    _current_jpg_width: int
    _current_jpg_height: int
    _last_jpeg_ts: int

    def __init__(
            self,
            frame_interval: int,
            video_callback: Callable[[bytes, int, int], Coroutine],
            audio_callback: Optional[Callable[[bytes, int, int], Coroutine]] = None,
            enable_hw_accel: bool = False,
            enable_audio: bool = False,
            main_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__()
        self._main_loop = main_loop or asyncio.get_running_loop()
        self._running = False
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        self._enable_audio = enable_audio

        self._video_callback = video_callback
        if enable_audio:
            if not audio_callback:
                raise MIoTMediaDecoderError("audio_callback is required")
            self._audio_callback = audio_callback

        self._queue = MIoTMediaRingBuffer()
        self._video_decoder = None
        self._audio_decoder = None
        self._resampler = None

        self._last_jpeg_ts = 0

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self._queue.step(
                    on_video_frame=self._on_video_callback,
                    on_audio_frame=self._on_audio_callback
                )
            except Exception as e:
                _LOGGER.error("frame data handle error, %s", e)
                if self._main_loop.is_closed(): break
        _LOGGER.info("decoder stopped")

    def stop(self) -> None:
        self._running = False
        self._queue.stop()
        self._video_decoder = None
        self._audio_decoder = None

    def push_video_frame(self, frame_data: MIoTCameraFrameData) -> None:
        self._queue.put_video(frame_data)

    def push_audio_frame(self, frame_data: MIoTCameraFrameData) -> None:
        self._queue.put_audio(frame_data)

    def _on_video_callback(self, frame_data: MIoTCameraFrameData) -> None:
        # Video decoding logic (kept for potential screenshot usage)
        if not self._video_decoder:
            try:
                if frame_data.codec_id == MIoTCameraCodec.VIDEO_H264:
                    self._video_decoder = VideoCodecContext.create("h264", "r")
                elif frame_data.codec_id == MIoTCameraCodec.VIDEO_H265:
                    self._video_decoder = VideoCodecContext.create("hevc", "r")
            except Exception:
                return

        try:
            pkt = Packet(frame_data.data)
            frames: List[VideoFrame] = self._video_decoder.decode(pkt)
            now_ts = int(time.time() * 1000)
            if now_ts - self._last_jpeg_ts >= self._frame_interval:
                if not frames:
                    self._last_jpeg_ts = now_ts
                    return
                frame = frames[0]
                rgb_frame: VideoFrame = frame.to_rgb()
                img: Image.Image = rgb_frame.to_image()
                buf: BytesIO = BytesIO()
                img.save(buf, format="JPEG", quality=90)
                jpeg_data = buf.getvalue()
                if self._video_callback:
                    self._main_loop.call_soon_threadsafe(
                        self._main_loop.create_task,
                        self._video_callback(jpeg_data, frame_data.timestamp, frame_data.channel)
                    )
                self._last_jpeg_ts = now_ts
        except Exception as e:
            _LOGGER.error("Decode video error: %s", e)

    def _on_audio_callback(self, frame_data: MIoTCameraFrameData) -> None:
        pcm_bytes = b""

        # [核心修正] G.711 A/U: 原生解码，并正确处理采样率
        if frame_data.codec_id in [MIoTCameraCodec.AUDIO_G711A, MIoTCameraCodec.AUDIO_G711U]:
            try:
                # 1. Decode G.711 -> PCM
                if frame_data.codec_id == MIoTCameraCodec.AUDIO_G711A:
                    pcm_bytes = audioop.alaw2lin(frame_data.data, 2)
                else:
                    pcm_bytes = audioop.ulaw2lin(frame_data.data, 2)

                # 2. [关键修改] 采样率处理
                # 你的摄像头是 16000Hz (G.711 Wideband)
                # 之前我们把 16k 当成 8k 输入，导致 audioop 把它拉长了2倍 (慢放)
                # 现在修正：输入=16000，输出=16000 (相当于直通，但保留 ratecv 以防万一)
                pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, 16000, 16000, None)

            except Exception as e:
                _LOGGER.error("G.711 decode error: %s", e)
                return

        elif frame_data.codec_id == MIoTCameraCodec.AUDIO_OPUS:
            if not self._audio_decoder:
                try:
                    self._audio_decoder = AudioCodecContext.create("opus", "r")
                    self._resampler = AudioResampler(format="s16", layout="mono", rate=16000)
                except Exception:
                    return
            try:
                pkt = Packet(frame_data.data)
                frames = self._audio_decoder.decode(pkt)
                for frame in frames:
                    rs_frames = self._resampler.resample(frame)
                    for rs_frame in rs_frames:
                        pcm_bytes += rs_frame.to_ndarray().tobytes()
            except Exception:
                return

        if pcm_bytes and self._audio_callback:
            self._main_loop.call_soon_threadsafe(
                self._main_loop.create_task,
                self._audio_callback(pcm_bytes, frame_data.timestamp, frame_data.channel)
            )