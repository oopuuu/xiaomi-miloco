# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MiOT service module
"""

import logging
from typing import List, Optional, Callable, Coroutine
from functools import partial

from miot.types import MIoTUserInfo, MIoTCameraInfo, MIoTDeviceInfo, MIoTManualSceneInfo
from miloco_server.proxy.miot_proxy import MiotProxy
from miloco_server.schema.trigger_schema import Action
from miloco_server.schema.miot_schema import CameraChannel, CameraImgSeq, CameraInfo, DeviceInfo, SceneInfo
from miloco_server.middleware.exceptions import (
    MiotOAuthException,
    MiotServiceException,
    ValidationException,
    BusinessException,
    ResourceNotFoundException
)
from miloco_server.utils.default_action import DefaultPresetActionManager
from miloco_server.mcp.mcp_client_manager import MCPClientManager
from miloco_server.utils.ffmpeg_streamer import FFmpegStreamer

logger = logging.getLogger(__name__)


class MiotService:
    """MiOT service class"""

    def __init__(self, miot_proxy: MiotProxy, mcp_client_manager: MCPClientManager,
                 default_preset_action_manager: Optional[DefaultPresetActionManager] = None):
        self._miot_proxy = miot_proxy
        self._mcp_client_manager = mcp_client_manager
        self._default_preset_action_manager = default_preset_action_manager
        self._streamers: dict[str, FFmpegStreamer] = {}

    @property
    def miot_client(self):
        return self._miot_proxy.miot_client

    async def process_xiaomi_home_callback(self, code: str, state: str):
        try:
            logger.info("process_xiaomi_home_callback code: %s, status: %s", code, state)
            await self._miot_proxy.get_miot_auth_info(code=code, state=state)
            await self._mcp_client_manager.init_miot_mcp_clients()
        except Exception as e:
            logger.error("Failed to process Xiaomi MiOT authorization code: %s", e)
            raise MiotServiceException(f"Failed to process Xiaomi MiOT authorization code: {str(e)}") from e

    async def refresh_miot_all_info(self) -> dict:
        try:
            return await self._miot_proxy.refresh_miot_info()
        except Exception as e:
            raise MiotServiceException(f"Failed to refresh MiOT all information: {str(e)}") from e

    async def refresh_miot_cameras(self):
        try:
            result = await self._miot_proxy.refresh_cameras()
            if not result: raise MiotServiceException("Failed to refresh MiOT cameras")
            return True
        except Exception as e:
            raise MiotServiceException(f"Failed to refresh MiOT cameras: {str(e)}") from e

    async def refresh_miot_scenes(self):
        try:
            result = await self._miot_proxy.refresh_scenes()
            if not result: raise MiotServiceException("Failed to refresh MiOT scenes")
            return True
        except Exception as e:
            raise MiotServiceException(f"Failed to refresh MiOT scenes: {str(e)}") from e

    async def refresh_miot_user_info(self):
        try:
            result = await self._miot_proxy.refresh_user_info()
            if not result: raise MiotServiceException("Failed to refresh MiOT user info")
            return True
        except Exception as e:
            raise MiotServiceException(f"Failed to refresh MiOT user info: {str(e)}") from e

    async def refresh_miot_devices(self):
        try:
            result = await self._miot_proxy.refresh_devices()
            if not result: raise MiotServiceException("Failed to refresh MiOT devices")
            return True
        except Exception as e:
            raise MiotServiceException(f"Failed to refresh MiOT devices: {str(e)}") from e

    async def get_miot_login_status(self) -> dict:
        try:
            is_token_valid = await self._miot_proxy.check_token_valid()
            if not is_token_valid:
                login_url = await self._miot_proxy.get_miot_login_url()
                return {"is_logged_in": False, "login_url": login_url}
            return {"is_logged_in": True}
        except Exception as e:
            raise MiotOAuthException(f"Failed to check MiOT login status: {str(e)}") from e

    async def get_miot_user_info(self) -> MIoTUserInfo:
        try:
            user_info = await self._miot_proxy.get_user_info()
            if not user_info: raise ResourceNotFoundException("No logged in user information found")
            return user_info
        except Exception as e:
            raise MiotServiceException(f"Failed to get MiOT user info: {str(e)}") from e

    async def get_miot_camera_list(self) -> List[CameraInfo]:
        try:
            camera_dict = await self._miot_proxy.get_cameras()
            if not camera_dict: raise MiotServiceException("Failed to get MiOT camera list")
            return [CameraInfo.model_validate(info.model_dump()) for info in camera_dict.values()]
        except Exception as e:
            raise MiotServiceException(f"Failed to get MiOT camera list: {str(e)}") from e

    async def get_miot_device_list(self) -> List[DeviceInfo]:
        try:
            device_dict = await self._miot_proxy.get_devices()
            if not device_dict: raise MiotServiceException("Failed to get MiOT device list")
            return [DeviceInfo.model_validate(info.model_dump()) for info in device_dict.values()]
        except Exception as e:
            raise MiotServiceException(f"Failed to get MiOT device list: {str(e)}") from e

    async def get_miot_cameras_img(self, camera_dids: list[str], vision_use_img_count: int) -> list[CameraImgSeq]:
        logger.info("get_miot_cameras_img, camera_dids: %s", ", ".join(camera_dids))
        try:
            all_camera_info = await self._miot_proxy.get_cameras()
            if not all_camera_info: return []

            camera_img_seqs = []
            for did, info in all_camera_info.items():
                if did in camera_dids:
                    for channel in range(info.channel_count or 1):
                        seq = self._miot_proxy.get_recent_camera_img(did, channel, vision_use_img_count)
                        if seq: camera_img_seqs.append(seq)
            return camera_img_seqs
        except Exception as e:
            raise MiotServiceException(f"Failed to get MiOT camera images: {str(e)}") from e

    async def get_miot_scene_list(self) -> List[SceneInfo]:
        try:
            scenes = await self._miot_proxy.get_all_scenes()
            if scenes is None: raise MiotServiceException("Failed to get MiOT scene list")
            return [SceneInfo(scene_id=s.scene_id, scene_name=s.scene_name) for s in scenes.values()]
        except Exception as e:
            raise MiotServiceException(f"Failed to get MiOT scene list: {str(e)}") from e

    async def send_notify(self, notify: str) -> None:
        try:
            notify_id = await self._miot_proxy.get_miot_app_notify_id(notify)
            if not notify_id: raise ValidationException("MiOT app notification content is inappropriate")
            result = await self._miot_proxy.send_app_notify(notify_id)
            if not result: raise BusinessException("Failed to send notification")
        except Exception as e:
            raise BusinessException(f"Failed to send notification: {str(e)}") from e

    # [关键修改] 启动流
    async def start_video_stream(self, camera_id: str, channel: int,
                                 callback: Callable[..., Coroutine],  # 保留兼容
                                 video_quality: int):
        try:
            logger.info(f"Starting Local RTSP for {camera_id}...")

            # 1. 确保 Camera Instance 存在 (同之前逻辑)
            camera_instance = self._miot_proxy.get_camera_instance(camera_id)
            if camera_instance and video_quality > 1:
                await self._miot_proxy.destroy_camera_proxy(camera_id)
                camera_instance = None
            if not camera_instance:
                await self._miot_proxy.create_camera_proxy(camera_id, target_quality=video_quality)
                camera_instance = self._miot_proxy.get_camera_instance(camera_id)

            # 2. 启动内部 Streamer
            if camera_id not in self._streamers:
                streamer = FFmpegStreamer(camera_id)
                streamer.start(video_codec="hevc")  # 假设是 HEVC
                self._streamers[camera_id] = streamer

            # 3. 定义数据回调：直接喂给 Streamer
            # 注意：p_type=1 是视频，2 是音频
            async def on_video(did, data, ts, seq, channel):
                if did in self._streamers: self._streamers[did].push_video(data)

            async def on_audio(did, data, ts, seq, channel):
                if did in self._streamers: self._streamers[did].push_audio(data)

            # 4. 注册到底层 (multi_reg=True 允许多个接收者)
            # 注意：这里我们不需要 partial 了，直接定义简单函数即可
            await camera_instance.register_raw_video_async(on_video, channel, True)
            await camera_instance.register_raw_audio_async(on_audio, channel, True)

            await camera_instance.start_async(qualities=video_quality, enable_audio=True, enable_reconnect=True)

            logger.info(f"RTSP Ready at: rtsp://YOUR_MILOCO_IP:8554/{camera_id}")

        except Exception as e:
            logger.error(f"Stream start failed: {e}")

    async def stop_video_stream(self, camera_id: str, channel: int, video_quality: int = None):
        try:
            logger.info("Stopping video stream: camera_id=%s", camera_id)
            await self._miot_proxy.stop_camera_raw_stream(camera_id, channel, video_quality)
        except Exception as e:
            logger.error("Failed to stop video stream: %s", e)
            raise MiotServiceException(f"Failed to stop video stream: {str(e)}") from e

    async def get_miot_scene_actions(self) -> List[Action]:
        try:
            if not self._default_preset_action_manager:
                raise MiotServiceException("DefaultPresetActionManager not initialized")
            actions = await self._default_preset_action_manager.get_miot_scene_actions()
            return list(actions.values())
        except Exception as e:
            raise MiotServiceException(f"Failed to get MiOT scene action list: {str(e)}") from e