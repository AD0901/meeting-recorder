"""
录音和纪要存哪里：.env 里 STORAGE=local / oss 切换。

  local  只留在服务端本机 data/ 目录（默认）
  oss    本机留一份，同时归档到阿里云 OSS（OSS_BUCKET / OSS_PREFIX）

注意：用通义听悟转写时，音频总要临时传到 OSS 给听悟读取（听悟只接受 URL），
这和 STORAGE 无关；STORAGE 只决定"长期归档"放在哪。
以后要接 MinIO / S3 / 腾讯 COS，在这里加一个类即可。
"""
from __future__ import annotations

import os
from pathlib import Path


class LocalStorage:
    name = "local"

    def put(self, key: str, path: Path) -> None:  # 文件本来就在 data/ 里
        pass


class OssStorage:
    name = "oss"

    def __init__(self) -> None:
        import oss2

        auth = oss2.Auth(os.environ["ALIYUN_ACCESS_KEY_ID"], os.environ["ALIYUN_ACCESS_KEY_SECRET"])
        self.bucket = oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], os.environ["OSS_BUCKET"])
        self.prefix = os.getenv("OSS_PREFIX", "meeting-recorder/")

    def put(self, key: str, path: Path) -> None:
        self.bucket.put_object_from_file(self.prefix + key, str(path))


def make_storage():
    kind = os.getenv("STORAGE", "local").strip().lower()
    if kind == "oss":
        return OssStorage()
    if kind in ("local", ""):
        return LocalStorage()
    raise RuntimeError(f"未知的 STORAGE={kind}（可选 local / oss）")
