from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from nga_tools.console import report_info, report_progress
from nga_tools.image_cluster.cluster import Cluster, build_clusters
from nga_tools.image_cluster.features import ImageFeatures, extract_features
from nga_tools.image_cluster.lsh import (
    LshConfig,
    generate_candidate_pairs,
)
from nga_tools.image_cluster.store import ImageClusterStore

_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".heif",
        ".heic",
        ".avif",
        ".jxl",
    }
)
_IMAGES_DIR_NAME = "images_unique"
_PROGRESS_INTERVAL = 100


@dataclass(frozen=True)
class ClusterParams:
    threshold: int = 1
    dhash_threshold: int = 2
    color_threshold: float = 0.1
    min_cluster_size: int = 2
    lsh_bands: int = 4
    workers: int = 0
    limit: int = 0
    force: bool = False


@dataclass(frozen=True)
class ClusterRunResult:
    run_id: int
    total_images: int
    features_computed: int
    features_reused: int
    features_deleted: int
    candidate_pairs: int
    cluster_count: int
    clustered_images: int


def default_workers() -> int:
    count = os.cpu_count()
    return count if count else 1


def _scan_images(images_dir: Path) -> list[tuple[Path, str]]:
    if not images_dir.is_dir():
        return []
    result: list[tuple[Path, str]] = []
    for path in sorted(images_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS:
            result.append((path, f"{_IMAGES_DIR_NAME}/{path.name}"))
    return result


def _extract_feature_task(
    args: tuple[str, str]
) -> ImageFeatures | None:
    path_str, relative_path = args
    return extract_features(Path(path_str), relative_path)


def _compute_features_sequential(
    tasks: list[tuple[str, str]],
) -> list[ImageFeatures]:
    result: list[ImageFeatures] = []
    total = len(tasks)
    for index, args in enumerate(tasks, 1):
        features = _extract_feature_task(args)
        if features is not None:
            result.append(features)
        if index % _PROGRESS_INTERVAL == 0 or index == total:
            report_progress("计算图片特征", completed=index, total=total)
    return result


def _compute_features_parallel(
    tasks: list[tuple[str, str]], workers: int
) -> list[ImageFeatures]:
    result: list[ImageFeatures] = []
    total = len(tasks)
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_extract_feature_task, args): args for args in tasks
        }
        for future in as_completed(futures):
            features = future.result()
            if features is not None:
                result.append(features)
            done += 1
            if done % _PROGRESS_INTERVAL == 0 or done == total:
                report_progress("计算图片特征", completed=done, total=total)
    return result


def run_image_cluster(
    output_dir: Path, params: ClusterParams
) -> ClusterRunResult:
    store = ImageClusterStore(output_dir)
    store.ensure_store()

    images_dir = output_dir / _IMAGES_DIR_NAME
    scanned = _scan_images(images_dir)
    if params.limit > 0:
        scanned = scanned[: params.limit]

    total_images = len(scanned)
    scanned_keys = {relative for _, relative in scanned}

    existing = (
        store.load_feature_fingerprints() if not params.force else {}
    )

    to_compute: list[tuple[str, str]] = []
    reused = 0
    for path, relative in scanned:
        try:
            stat = path.stat()
        except OSError:
            continue
        if (
            not params.force
            and existing.get(relative) == (stat.st_size, stat.st_mtime_ns)
        ):
            reused += 1
        else:
            to_compute.append((str(path), relative))

    deleted_paths = set(existing.keys()) - scanned_keys
    if deleted_paths and not params.force:
        store.delete_features(deleted_paths)

    computed = 0
    if to_compute:
        if params.workers <= 1:
            new_features = _compute_features_sequential(to_compute)
        else:
            new_features = _compute_features_parallel(
                to_compute, params.workers
            )
        store.upsert_features(new_features)
        computed = len(new_features)

    all_features = store.load_all_features()
    hashes = {path: f.phash for path, f in all_features.items()}

    report_info("生成 LSH 候选对...")
    config = LshConfig(bands=params.lsh_bands)
    pairs = generate_candidate_pairs(hashes, config)

    report_info(
        f"聚类（{len(pairs)} 候选对，pHash阈值 {params.threshold}，"
        f"dHash阈值 {params.dhash_threshold}，"
        f"颜色阈值 {params.color_threshold}）..."
    )
    clusters: list[Cluster] = build_clusters(
        all_features,
        pairs,
        params.threshold,
        params.min_cluster_size,
        params.dhash_threshold,
        params.color_threshold,
    )

    params_dict: dict[str, object] = {
        "threshold": params.threshold,
        "dhash_threshold": params.dhash_threshold,
        "color_threshold": params.color_threshold,
        "min_cluster_size": params.min_cluster_size,
        "lsh_bands": params.lsh_bands,
        "force": params.force,
    }
    run_id = store.save_run(params_dict, clusters)

    clustered_images = sum(len(c.members) for c in clusters)

    return ClusterRunResult(
        run_id=run_id,
        total_images=total_images,
        features_computed=computed,
        features_reused=reused,
        features_deleted=len(deleted_paths) if not params.force else 0,
        candidate_pairs=len(pairs),
        cluster_count=len(clusters),
        clustered_images=clustered_images,
    )
