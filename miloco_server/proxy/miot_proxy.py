# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIoT proxy module for handling Xiaomi IoT device related operations."""

import asyncio
import copy
import json
import logging
import time
from typing import Callable, Coroutine, Optional, List, Dict, Set

from pydantic_core import to_jsonable_python
from miot.client import MIoTClient
from miot.types import MIoTOauthInfo, MIoTCameraInfo, MIoTDeviceInfo, MIoTManualSceneInfo, MIoTUserInfo, \
    MIoTCameraVideoQuality, MIoTCameraStatus
from miot.camera import MIoTCameraInstance

from miloco_server.config import MIOT_CACHE_DIR, CAMERA_CONFIG
from miloco_server.dao.kv_dao import AuthConfigKeys, KVDao, DeviceInfoKeys
from miloco_server.schema.miot_schema import CameraImgSeq
from miloco_server.utils.carmera_vision_handler import CameraVisionHandler

logger = logging.getLogger(__name__)


class MiotProxy:
    """Xiaomi IoT proxy class responsible for handling MIoT device related operations."""

    def __init__(self,
                 uuid: str,
                 redirect_uri: str,
                 kv_dao: KVDao,
                 cloud_server: Optional[str] = None,
                 ):
        self._kv_dao = kv_dao
        self.init_miot_info_dict()

        # Key: did
        self._camera_img_managers: dict[str, CameraVisionHandler] = {}
        self._stream_subscribers: Dict[str, Set[Callable]] = {}
        self._token_refresh_task: Optional[asyncio.Task] = None

        self._miot_client = MIoTClient(
            uuid=uuid,
            redirect_uri=redirect_uri,
            cache_path=str(MIOT_CACHE_DIR),
            oauth_info=self._oauth_info,
            cloud_server=cloud_server,
        )

        self._frame_interval: int = CAMERA_CONFIG["frame_interval"]
        self._camera_img_cache_max_size: int = CAMERA_CONFIG["camera_img_cache_max_size"]
        self._camera_img_cache_ttl: int = max(1, int(self._frame_interval * self._camera_img_cache_max_size / 1000 * 2))

    @property
    def miot_client(self) -> MIoTClient:
        return self._miot_client

    def get_camera_instance(self, did: str) -> Optional[MIoTCameraInstance]:
        if did in self._camera_img_managers:
            return self._camera_img_managers[did].miot_camera_instance
        return None

    def get_camera_vision_handler(self, did: str) -> Optional[CameraVisionHandler]:
        return self._camera_img_managers.get(did)

    async def destroy_camera_proxy(self, did: str):
        if did in self._camera_img_managers:
            logger.info(f"Destroying camera proxy for {did}...")
            try:
                await self._camera_img_managers[did].destroy()
            except Exception as e:
                logger.warning(f"Error destroying camera {did}: {e}")
            finally:
                self._camera_img_managers.pop(did, None)
            await asyncio.sleep(0.5)

    @classmethod
    async def create_miot_proxy(cls, uuid: str, redirect_uri: str, kv_dao: KVDao,
                                cloud_server: Optional[str] = None) -> "MiotProxy":
        instance = cls(uuid, redirect_uri, kv_dao, cloud_server)
        await instance.init_miot_info()
        instance._token_refresh_task = asyncio.create_task(instance._start_token_refresh_task())
        logger.info("MiotProxy initialized successful")
        return instance

    async def init_miot_info(self):
        await self._miot_client.init_async()
        if self._oauth_info:
            await self._check_and_refresh_token()
            await self.refresh_miot_info()

    async def refresh_miot_info(self) -> dict:
        result = {"cameras": False, "scenes": False, "user_info": False, "devices": False}

        results = await asyncio.gather(
            self.refresh_cameras(),
            self.refresh_scenes(),
            self.refresh_user_info(),
            self.refresh_devices(),
            return_exceptions=True
        )

        result["cameras"] = not isinstance(results[0], Exception) and results[0] is not None
        result["scenes"] = not isinstance(results[1], Exception) and results[1] is not None
        result["user_info"] = not isinstance(results[2], Exception) and results[2] is not None
        result["devices"] = not isinstance(results[3], Exception) and results[3] is not None

        logger.info("MiOT info refresh completed: %s", result)
        return result

    def init_miot_info_dict(self):
        try:
            self._camera_info_dict: dict[str, MIoTCameraInfo] = {
                did: MIoTCameraInfo.model_validate(camera_info)
                for did, camera_info in json.loads(self._kv_dao.get(DeviceInfoKeys.CAMERA_INFO_KEY) or "{}").items()}

            self._device_info_dict: dict[str, MIoTDeviceInfo] = {
                did: MIoTDeviceInfo.model_validate(device_info)
                for did, device_info in json.loads(self._kv_dao.get(DeviceInfoKeys.DEVICE_INFO_KEY) or "{}").items()}

            self._scene_info_dict: dict[str, MIoTManualSceneInfo] = {
                scene_id: MIoTManualSceneInfo.model_validate(scene_info)
                for scene_id, scene_info in json.loads(self._kv_dao.get(DeviceInfoKeys.SCENE_INFO_KEY) or "{}").items()}

            user_info_str = self._kv_dao.get(DeviceInfoKeys.USER_INFO_KEY)
            self._user_info = MIoTUserInfo.model_validate_json(user_info_str) if user_info_str else None

            oauth_info_str = self._kv_dao.get(AuthConfigKeys.MIOT_TOKEN_INFO_KEY)
            self._oauth_info = MIoTOauthInfo.model_validate_json(oauth_info_str) if oauth_info_str else None
        except Exception as e:
            logger.error(f"Failed to load cached info from KV: {e}")
            self._camera_info_dict = {}
            self._device_info_dict = {}
            self._scene_info_dict = {}
            self._user_info = None
            self._oauth_info = None

    def get_recent_camera_img(self, camera_id: str, channel: int, recent_count: int) -> CameraImgSeq | None:
        if camera_id in self._camera_img_managers:
            return self._camera_img_managers[camera_id].get_recents_camera_img(channel, recent_count)
        return None

    async def create_camera_proxy(self, did: str, target_quality: int = None):
        if did in self._camera_img_managers:
            return

        if not self._camera_info_dict or did not in self._camera_info_dict:
            await self.refresh_cameras()

        if did in self._camera_info_dict:
            q = target_quality if target_quality is not None else MIoTCameraVideoQuality.HIGH.value
            await self._create_camera_img_manager(
                self._camera_info_dict[did],
                target_quality=q
            )
        else:
            logger.warning(f"Cannot create proxy for unknown camera: {did}")

    async def _master_stream_callback(self, did: str, data: bytes, ts: int, seq: int, channel: int, frame_type: int = None):
        if did in self._stream_subscribers:
            subscribers = list(self._stream_subscribers[did])
            for callback in subscribers:
                try:
                    # 注意：这里的下游 callback 可能也没更新签名
                    # 如果下游 callback (比如 WS) 不需要 frame_type，我们就不传给它，或者由下游自己处理
                    # 目前主要目的是防止这里 crash
                    await callback(did, data, ts, seq, channel)
                except Exception as e:
                    logger.error("Error in subscriber callback for %s: %s", did, e)

    async def _dummy_audio_callback(self, did: str, data: bytes, ts: int, seq: int, channel: int):
        pass

    async def start_camera_raw_stream(self, camera_id: str, channel: int,
                                      callback: Callable, video_quality: int):
        logger.info("[Legacy Stream] Start Request: DID=%s", camera_id)

    async def stop_camera_raw_stream(self, camera_id: str, channel: int, video_quality: int = None):
        logger.info("[Stream] Stop Request (Unsubscribe): DID=%s", camera_id)

    async def _on_device_status_changed(self, did: str, status: MIoTCameraStatus):
        if did in self._camera_info_dict:
            if status.value > 0:
                self._camera_info_dict[did].online = True
                self._camera_info_dict[did].camera_status = status

    async def _create_camera_img_manager(self, camera_info: MIoTCameraInfo,
                                         target_quality: int = None) -> CameraVisionHandler | None:
        quality_val = target_quality if target_quality is not None else MIoTCameraVideoQuality.HIGH.value
        camera_info_copy = copy.deepcopy(camera_info)
        camera_info_copy.video_quality = quality_val

        logger.info("[Proxy] Creating connection for %s (Q=%s)...", camera_info.did, quality_val)

        try:
            camera_instance = await self._get_camera_instance(camera_info_copy)
        except Exception as e:
            logger.error("Failed to create instance: %s", e)
            return None

        if camera_instance is not None:
            await camera_instance.register_status_changed_async(self._on_device_status_changed)

            await camera_instance.start_async(enable_reconnect=True, qualities=quality_val, enable_audio=True)

            await camera_instance.register_raw_audio_async(self._dummy_audio_callback, 0)

            camera_img_manager = CameraVisionHandler(
                camera_info_copy, camera_instance, max_size=self._camera_img_cache_max_size,
                ttl=self._camera_img_cache_ttl
            )
            self._camera_img_managers[camera_info.did] = camera_img_manager
            return camera_img_manager
        return None

    async def _get_camera_instance(self, camera_info: MIoTCameraInfo, target_quality: int = None) -> Optional[
        MIoTCameraInstance]:
        try:
            return await self._miot_client.create_camera_instance_async(
                camera_info, frame_interval=self._frame_interval
            )
        except Exception as e:
            logger.error("Failed to get camera instance: %s", e)
            return None

    # ==========================================================================
    #  Getters & Refreshers
    # ==========================================================================

    async def get_cameras(self) -> dict[str, MIoTCameraInfo]:
        if not self._camera_info_dict:
            await self.refresh_cameras()
        return self._camera_info_dict

    async def refresh_cameras(self) -> dict[str, MIoTCameraInfo] | None:
        logger.info("[Refresh] Refreshing cameras from Cloud...")
        try:
            cameras = await self._miot_client.get_cameras_async()
            cameras = copy.deepcopy(cameras)

            for did, info in cameras.items():
                if did in self._camera_img_managers:
                    mgr = self._camera_img_managers[did]
                    try:
                        if hasattr(mgr, "miot_camera_instance"):
                            status = await mgr.miot_camera_instance.get_status_async()
                            if status.value > 0:
                                info.online = True
                                info.camera_status = status
                    except Exception:
                        pass

            self._camera_info_dict = cameras
            self._kv_dao.set(DeviceInfoKeys.CAMERA_INFO_KEY, json.dumps(to_jsonable_python(cameras)))

            for did, manager in self._camera_img_managers.items():
                if did in cameras: await manager.update_camera_info(cameras[did])

            dids_to_remove = []
            for did in list(self._camera_img_managers.keys()):
                if did not in cameras:
                    await self._camera_img_managers[did].destroy()
                    dids_to_remove.append(did)
                    if did in self._stream_subscribers: del self._stream_subscribers[did]
            for k in dids_to_remove: del self._camera_img_managers[k]

            # [关键恢复] 自动保活逻辑
            # 对所有摄像头建立 Low Quality 连接，确保在线状态和缩略图功能
            for camera_did in cameras.keys():
                if camera_did not in self._camera_img_managers:
                    logger.info("[Refresh] Auto-connecting %s (Q=1) for Keep-Alive", camera_did)
                    await self._create_camera_img_manager(cameras[camera_did], target_quality=1)
                    if camera_did in self._camera_img_managers:
                        # 注册主回调以消耗视频数据
                        await self._camera_img_managers[camera_did].register_raw_stream(self._master_stream_callback, 0)

            return cameras
        except Exception as e:
            logger.error("Failed to refresh cameras: %s", e)
            return None

    # ... (其余方法保持不变) ...
    async def get_camera_dids(self) -> list[str]:
        camera_dict = await self.get_cameras()
        return list(camera_dict.keys()) if camera_dict else []

    async def get_devices(self) -> dict[str, MIoTDeviceInfo]:
        if not self._device_info_dict:
            await self.refresh_devices()
        return self._device_info_dict

    async def refresh_devices(self) -> dict[str, MIoTDeviceInfo] | None:
        devices = await self._miot_client.get_devices_async()
        self._device_info_dict = devices
        self._kv_dao.set(DeviceInfoKeys.DEVICE_INFO_KEY, json.dumps(to_jsonable_python(devices)))
        return devices

    async def refresh_scenes(self) -> dict[str, MIoTManualSceneInfo] | None:
        scenes = await self._miot_client.get_manual_scenes_async()
        self._scene_info_dict = scenes
        self._kv_dao.set(DeviceInfoKeys.SCENE_INFO_KEY, json.dumps(to_jsonable_python(scenes)))
        return scenes

    async def get_all_scenes(self) -> dict[str, MIoTManualSceneInfo]:
        if not self._scene_info_dict:
            await self.refresh_scenes()
        return self._scene_info_dict

    async def refresh_user_info(self):
        user_info = await self._miot_client.get_user_info_async()
        self._user_info = user_info
        self._kv_dao.set(DeviceInfoKeys.USER_INFO_KEY, json.dumps(to_jsonable_python(user_info)))
        return user_info

    async def get_user_info(self) -> Optional[MIoTUserInfo]:
        if not self._user_info:
            await self.refresh_user_info()
        return self._user_info

    async def _start_token_refresh_task(self):
        while True:
            try:
                await asyncio.sleep(300)
                await self._check_and_refresh_token()
            except Exception as e:
                logger.error(f"Token refresh task error: {e}")
                await asyncio.sleep(60)

    async def _check_and_refresh_token(self):
        if self._oauth_info and self._oauth_info.expires_ts - int(time.time()) <= 1800:
            await self.refresh_xiaomi_home_token_info()

    async def execute_miot_scene(self, scene_id):
        if scene_id not in self._scene_info_dict: await self.refresh_scenes()
        if scene_id in self._scene_info_dict:
            return await self._miot_client.run_manual_scene_async(self._scene_info_dict[scene_id])
        return False

    async def send_app_notify(self, nid):
        return await self._miot_client.send_app_notify_async(nid)

    async def check_token_valid(self):
        return await self._miot_client.check_token_async()

    async def get_miot_login_url(self):
        return await self._miot_client.gen_oauth_url_async()

    async def get_miot_app_notify_id(self, c):
        return await self._miot_client.http_client.create_app_notify_async(c)

    async def get_miot_auth_info(self, code, state):
        info = await self._miot_client.get_access_token_async(code, state)
        self.reset_miot_token_info(info)
        asyncio.create_task(self.refresh_miot_info())
        return info

    def reset_miot_token_info(self, info):
        self._oauth_info = info
        self._kv_dao.set(AuthConfigKeys.MIOT_TOKEN_INFO_KEY, info.model_dump_json())
        if self._miot_client.http_client: self._miot_client.http_client.access_token = info.access_token

    async def refresh_xiaomi_home_token_info(self) -> MIoTOauthInfo:
            try:
                if not self._oauth_info:
                    raise ValueError("No oauth_info found")
                oauth_info = await self._miot_client.refresh_access_token_async(
                    refresh_token=self._oauth_info.refresh_token
                )
                logger.info("Successfully refreshed Xiaomi home token info: %s", oauth_info)
                self.reset_miot_token_info(oauth_info)
                await asyncio.sleep(3)
                await self.refresh_miot_info()
                return oauth_info
            except Exception as e: # pylint: disable=broad-exception-caught
                self._oauth_info = None
                logger.error("Failed to refresh Xiaomi home token info: %s", e, exc_info=True)