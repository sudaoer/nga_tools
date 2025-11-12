import config
import os


def get_folder(tid, aid, subfolder=None):
    folder = config.OUTPUT_DIR + "/" + tid + "_" + (aid if aid else "all")
    if subfolder:
        folder += "/" + subfolder
    os.makedirs(folder, exist_ok=True)
    return folder


def list_files_in_folder(folder: str, ends_with: str = "") -> list[str]:
    """
    列出指定文件夹中的所有文件
    """
    if not os.path.exists(folder):
        return []
    return [
        f
        for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f)) and f.endswith(ends_with)
    ]


# 从bbcode统计字数
import re


def delete_bbcode_tags(text: str) -> str:
    """
    删除文本中的BBCode标签
    """
    # 定义BBCode标签的正则表达式模式
    bbcode_pattern = re.compile(r"\[/?[a-zA-Z]+(?:=[^\]]+)?\]")
    # 使用正则表达式替换BBCode标签为空字符串
    cleaned_text = bbcode_pattern.sub("", text)
    return cleaned_text

import sys
def TODO(message: str):
    """
    标记待办事项
    """
    print(f"TODO: {message}")
    sys.exit(1)


if __name__ == "__main__":
    sample_text = "[b]Bold Text[/b] and [url=http://example.com]Example Link[/url]"
    cleaned_text = delete_bbcode_tags(sample_text)
    print("Original Text:", sample_text)
    print("Cleaned Text:", cleaned_text)
    print("Word Count:", len(cleaned_text.split()))
