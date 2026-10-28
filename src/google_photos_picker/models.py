"""Typed views over Picker API resources (the raw JSON stays available as ``.raw``)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
MAX_ITEMS = 2000


def _duration(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    m = re.match(r"^(-?\d+(?:\.\d+)?)s$", str(value))
    if not m:
        raise ValueError(f"invalid duration {value!r}")
    return float(m.group(1))


def _timestamp(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    s = value.replace("Z", "+00:00")
    # Python < 3.11 only accepts exactly 3 or 6 fractional digits: normalise to 6.
    m = re.match(r"^([^.]*)\.(\d+)(.*)$", s)
    if m:
        s = f"{m.group(1)}.{(m.group(2) + '000000')[:6]}{m.group(3)}"
    return dt.datetime.fromisoformat(s)


@dataclass(frozen=True)
class PollingConfig:
    #: Recommended seconds between ``sessions.get`` polls.
    poll_interval: Optional[float]
    #: Seconds left to keep polling (0 = stop polling).
    timeout_in: Optional[float]


@dataclass(frozen=True)
class PickingSession:
    id: str
    picker_uri: str
    media_items_set: bool
    expire_time: Optional[dt.datetime]
    polling: Optional[PollingConfig]
    max_item_count: Optional[int]
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def picker_uri_autoclose(self) -> str:
        """``pickerUri`` + ``/autoclose`` – closes the Google Photos tab when done (web)."""
        return self.picker_uri.rstrip("/") + "/autoclose"

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> PickingSession:
        pc = d.get("pollingConfig")
        polling = PollingConfig(_duration(pc.get("pollInterval")),
                                _duration(pc.get("timeoutIn"))) if pc else None
        mic = (d.get("pickingConfig") or {}).get("maxItemCount")
        return cls(
            id=str(d["id"]),
            picker_uri=str(d.get("pickerUri", "")),
            media_items_set=bool(d.get("mediaItemsSet", False)),
            expire_time=_timestamp(d.get("expireTime")),
            polling=polling,
            max_item_count=int(mic) if mic not in (None, "") else None,
            raw=d,
        )


@dataclass(frozen=True)
class PhotoMetadata:
    focal_length: Optional[float] = None
    aperture_f_number: Optional[float] = None
    iso_equivalent: Optional[int] = None
    exposure_time: Optional[float] = None


@dataclass(frozen=True)
class VideoMetadata:
    fps: Optional[float] = None
    #: UNSPECIFIED | PROCESSING | READY | FAILED
    processing_status: Optional[str] = None


@dataclass(frozen=True)
class PickedMediaItem:
    id: str
    type: str  # PHOTO | VIDEO | TYPE_UNSPECIFIED
    create_time: Optional[dt.datetime]
    base_url: str
    mime_type: Optional[str]
    filename: Optional[str]
    width: Optional[int]
    height: Optional[int]
    camera_make: Optional[str]
    camera_model: Optional[str]
    photo: Optional[PhotoMetadata]
    video: Optional[VideoMetadata]
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_video(self) -> bool:
        return self.type == "VIDEO"

    @property
    def is_photo(self) -> bool:
        return self.type == "PHOTO"

    @property
    def ready(self) -> bool:
        """False for videos that are still processing or failed."""
        if not self.is_video:
            return True
        return bool(self.video and self.video.processing_status == "READY")

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> PickedMediaItem:
        mf = d.get("mediaFile") or {}
        md = mf.get("mediaFileMetadata") or {}
        pm = md.get("photoMetadata")
        vm = md.get("videoMetadata")
        photo = PhotoMetadata(
            focal_length=pm.get("focalLength"),
            aperture_f_number=pm.get("apertureFNumber"),
            iso_equivalent=pm.get("isoEquivalent"),
            exposure_time=_duration(pm.get("exposureTime")),
        ) if pm is not None else None
        video = VideoMetadata(fps=vm.get("fps"), processing_status=vm.get("processingStatus")
                              ) if vm is not None else None
        return cls(
            id=str(d["id"]),
            type=str(d.get("type", "TYPE_UNSPECIFIED")),
            create_time=_timestamp(d.get("createTime")),
            base_url=str(mf.get("baseUrl", "")),
            mime_type=mf.get("mimeType"),
            filename=mf.get("filename"),
            width=md.get("width"),
            height=md.get("height"),
            camera_make=md.get("cameraMake"),
            camera_model=md.get("cameraModel"),
            photo=photo,
            video=video,
            raw=d,
        )


def media_url(
    item: PickedMediaItem,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    crop: bool = False,
    video_overlay: bool = True,
) -> str:
    """Build a ``baseUrl`` download URL.

    * no size → original bytes: ``=d`` for photos (EXIF kept, location stripped),
      ``=dv`` for videos (high-quality transcode);
    * ``width``/``height`` → resized image or video thumbnail (``=wN-hN[-c][-no]``).

    Remember: base URLs need an ``Authorization: Bearer`` header and expire after ~60 min.
    """
    if not item.base_url:
        raise ValueError(f"media item {item.id} has no baseUrl")
    if width is None and height is None:
        if crop or not video_overlay:
            raise ValueError("crop / video_overlay require width and height")
        return f"{item.base_url}={'dv' if item.is_video else 'd'}"
    if width is None or height is None:
        raise ValueError("width and height must be given together")
    for v in (width, height):
        if not 1 <= v <= 16383:
            raise ValueError("width/height must be between 1 and 16383")
    opts = f"w{width}-h{height}"
    if crop:
        opts += "-c"
    if not video_overlay:
        opts += "-no"
    return f"{item.base_url}={opts}"
