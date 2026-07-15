from __future__ import annotations

from nga_tools.commands.types import CommandArgs, optional_int
from nga_tools.config import AppConfig, get_config
from nga_tools.network_limits import configure_network_limits


def configure_network_limits_from_args(args: CommandArgs) -> AppConfig:
    app_config = get_config()
    api_arg = optional_int(args, "api_concurrency")
    image_arg = optional_int(args, "image_concurrency")
    audio_arg = optional_int(args, "audio_concurrency")
    api_concurrency = app_config.api_concurrency if api_arg is None else api_arg
    image_concurrency = (
        app_config.image_concurrency if image_arg is None else image_arg
    )
    audio_concurrency = (
        app_config.audio_concurrency if audio_arg is None else audio_arg
    )
    configure_network_limits(
        api_concurrency=api_concurrency,
        image_concurrency=image_concurrency,
        audio_concurrency=audio_concurrency,
    )
    return app_config
