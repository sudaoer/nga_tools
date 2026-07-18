from __future__ import annotations

from pathlib import Path

from nga_tools.commands.types import (
    CommandArgs,
    optional_bool,
    optional_float,
    optional_int,
)
from nga_tools.config import get_config
from nga_tools.console import report_info
from nga_tools.image_cluster import ClusterParams, default_workers, run_image_cluster


def cluster_run(args: CommandArgs) -> None:
    threshold = optional_int(args, "threshold")
    dhash_threshold = optional_int(args, "dhash_threshold")
    color_threshold = optional_float(args, "color_threshold")
    detail_threshold = optional_float(args, "detail_threshold")
    min_cluster_size = optional_int(args, "min_cluster_size")
    lsh_bands = optional_int(args, "lsh_bands")
    workers = optional_int(args, "workers")
    limit = optional_int(args, "limit")
    force = optional_bool(args, "force")

    params = ClusterParams(
        threshold=threshold if threshold is not None and threshold > 0 else 1,
        dhash_threshold=(
            dhash_threshold
            if dhash_threshold is not None and dhash_threshold > 0
            else 2
        ),
        color_threshold=(
            color_threshold
            if color_threshold is not None and color_threshold >= 0
            else 0.05
        ),
        detail_threshold=(
            detail_threshold
            if detail_threshold is not None and detail_threshold >= 0
            else 0.18
        ),
        min_cluster_size=(
            min_cluster_size
            if min_cluster_size is not None and min_cluster_size > 0
            else 2
        ),
        lsh_bands=lsh_bands if lsh_bands is not None and lsh_bands > 0 else 4,
        workers=(
            workers if workers is not None and workers > 0 else default_workers()
        ),
        limit=limit if limit is not None and limit > 0 else 0,
        force=force,
    )

    output_dir = Path(get_config().output_dir)
    result = run_image_cluster(output_dir, params)
    report_info(f"图片聚类完成：run_id={result.run_id}")
    report_info(f"  扫描图片：{result.total_images}")
    report_info(f"  新算特征：{result.features_computed}")
    report_info(f"  复用特征：{result.features_reused}")
    report_info(f"  删除特征：{result.features_deleted}")
    report_info(f"  候选对数：{result.candidate_pairs}")
    report_info(f"  粗簇数量：{result.coarse_cluster_count}")
    report_info(f"  细分配对：{result.detail_pairs}")
    report_info(f"  新算细分分数：{result.detail_scores_computed}")
    report_info(f"  复用细分分数：{result.detail_scores_reused}")
    report_info(f"  聚类数量：{result.cluster_count}")
    report_info(f"  聚类图片：{result.clustered_images}")
