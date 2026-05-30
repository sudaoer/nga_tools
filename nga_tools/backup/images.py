from __future__ import annotations

import os
from typing import Optional

from PIL import Image

from nga_tools import utils


def verify_downloaded_images(tid: int, aid: Optional[int]) -> None:
    folder_images = utils.get_folder(tid, aid, "images")
    image_files = utils.list_files_in_folder(folder_images)
    print(f"已下载图片文件数：{len(image_files)}")

    for image_file in image_files:
        image_path = f"{folder_images}/{image_file}"
        try:
            with Image.open(image_path) as image:
                image.verify()
        except (OSError, SyntaxError) as error:
            print(f"图片文件损坏或无法打开：{image_file}，错误信息：{error}")
            os.remove(image_path)
