import json

import config


class NGAThreadConfigs:
    def __init__(self):
        self.ThreadList = []
        self.config_file_path = config.THREAD_CONFIG_FILE
        self.load_configs()

    def load_configs(self):
        try:
            with open(self.config_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.ThreadList = data.get("ThreadList", [])
        except FileNotFoundError:
            self.ThreadList = []

    def add_thread(
        self, thread_name: str, tid: int, aid: int | None = None, description: str = ""
    ):
        thread_config = {
            "thread_name": thread_name,
            "tid": tid,
            "aid": aid,
            "description": description,
        }
        for existing in self.ThreadList:
            if existing["tid"] == tid and existing.get("aid") == aid:
                print("该帖子配置已存在，跳过添加。")
                return
        self.ThreadList.append(thread_config)

    def save_configs(self):
        data = {"ThreadList": self.ThreadList}
        with open(self.config_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def get_thread_configs(self):
        return self.ThreadList
