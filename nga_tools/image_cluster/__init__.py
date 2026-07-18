from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from nga_tools.console import report_info, report_progress
from nga_tools.image_cluster.cluster import (
    Cluster,
    build_coarse_groups,
    clusters_from_groups,
)
from nga_tools.image_cluster.detail import (
    DETAIL_SCORE_ALGORITHM,
    DetailPairScore,
    PairKey,
    complete_linkage_groups,
    detail_difference_score,
    pair_key,
    prepare_detail_image,
)
from nga_tools.image_cluster.features import (
    IMAGE_HASH_ALGORITHM,
    ImageFeatures,
    extract_features,
)
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
_DETAIL_PROGRESS_INTERVAL = 1_000
_DETAIL_SCORE_WRITE_BATCH = 2_000


@dataclass(frozen=True)
class ClusterParams:
    threshold: int = 1
    dhash_threshold: int = 2
    color_threshold: float = 0.05
    detail_threshold: float = 0.18
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
    coarse_cluster_count: int
    detail_pairs: int
    detail_scores_computed: int
    detail_scores_reused: int
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


def _detail_pairs(members: list[str]) -> list[PairKey]:
    return [
        pair_key(members[i], members[j])
        for i in range(len(members) - 1)
        for j in range(i + 1, len(members))
    ]


def _compute_detail_scores(
    output_dir: Path,
    missing_pairs: list[PairKey],
) -> dict[PairKey, float]:
    paths = sorted({path for pair in missing_pairs for path in pair})
    prepared = {
        path: prepare_detail_image(output_dir / path)
        for path in paths
    }
    return {
        key: detail_difference_score(prepared.get(key[0]), prepared.get(key[1]))
        for key in missing_pairs
    }


def _score_record(
    key: PairKey,
    score: float,
    features: dict[str, ImageFeatures],
) -> DetailPairScore:
    feature_a = features[key[0]]
    feature_b = features[key[1]]
    return DetailPairScore(
        path_a=key[0],
        path_b=key[1],
        size_a=feature_a.size,
        mtime_ns_a=feature_a.mtime_ns,
        size_b=feature_b.size,
        mtime_ns_b=feature_b.mtime_ns,
        algorithm=DETAIL_SCORE_ALGORITHM,
        score=score,
    )


def _refine_coarse_groups(
    output_dir: Path,
    store: ImageClusterStore,
    features: dict[str, ImageFeatures],
    coarse_groups: list[list[str]],
    *,
    detail_threshold: float,
    min_cluster_size: int,
    force: bool,
) -> tuple[list[list[str]], int, int, int]:
    eligible = [
        group for group in coarse_groups if len(group) >= min_cluster_size
    ]
    total_pairs = sum(len(group) * (len(group) - 1) // 2 for group in eligible)
    cached = (
        {}
        if force
        else store.load_detail_pair_scores(DETAIL_SCORE_ALGORITHM, features)
    )

    refined: list[list[str]] = []
    write_buffer: list[DetailPairScore] = []
    computed_count = 0
    reused_count = 0
    completed_pairs = 0
    total_groups = len(eligible)

    for index, group in enumerate(eligible, start=1):
        keys = _detail_pairs(group)
        group_scores = {key: cached[key] for key in keys if key in cached}
        reused_count += len(group_scores)
        missing = [key for key in keys if key not in group_scores]
        if missing:
            computed = _compute_detail_scores(output_dir, missing)
            group_scores.update(computed)
            computed_count += len(computed)
            write_buffer.extend(
                _score_record(key, score, features)
                for key, score in computed.items()
            )
            if len(write_buffer) >= _DETAIL_SCORE_WRITE_BATCH:
                store.upsert_detail_pair_scores(write_buffer)
                write_buffer.clear()

        refined.extend(
            complete_linkage_groups(group, group_scores, detail_threshold)
        )
        completed_pairs += len(keys)
        if index % _DETAIL_PROGRESS_INTERVAL == 0 or index == total_groups:
            report_progress(
                "细分粗聚类",
                completed=completed_pairs,
                total=total_pairs,
            )

    if write_buffer:
        store.upsert_detail_pair_scores(write_buffer)

    return refined, total_pairs, computed_count, reused_count


def run_image_cluster(
    output_dir: Path, params: ClusterParams
) -> ClusterRunResult:
    store = ImageClusterStore(output_dir)
    store.ensure_store()

    images_dir = output_dir / _IMAGES_DIR_NAME
    all_scanned = _scan_images(images_dir)
    all_scanned_keys = {relative for _, relative in all_scanned}
    scanned = all_scanned
    if params.limit > 0:
        scanned = scanned[: params.limit]

    total_images = len(scanned)
    scanned_keys = {relative for _, relative in scanned}

    invalidated = store.invalidate_features_if_hash_algorithm_changed(
        IMAGE_HASH_ALGORITHM
    )
    if invalidated:
        report_info(
            f"哈希算法已变更，清空旧图片特征缓存：{invalidated} 条"
        )

    existing = store.load_feature_fingerprints()

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

    deleted_paths = set(existing.keys()) - all_scanned_keys
    if deleted_paths:
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
    all_features = {
        path: feature
        for path, feature in all_features.items()
        if path in scanned_keys
    }
    hashes = {path: f.phash for path, f in all_features.items()}

    report_info("生成 LSH 候选对...")
    config = LshConfig(bands=params.lsh_bands)
    pairs = generate_candidate_pairs(hashes, config)

    report_info(
        f"聚类（{len(pairs)} 候选对，pHash阈值 {params.threshold}，"
        f"dHash阈值 {params.dhash_threshold}，"
        f"颜色阈值 {params.color_threshold}）..."
    )
    coarse_groups = build_coarse_groups(
        all_features,
        pairs,
        params.threshold,
        params.dhash_threshold,
        params.color_threshold,
    )
    coarse_cluster_count = sum(
        len(group) >= params.min_cluster_size for group in coarse_groups
    )

    report_info(
        f"细分 {coarse_cluster_count} 个粗簇（局部差异阈值 "
        f"{params.detail_threshold}，complete linkage）..."
    )
    refined_groups, detail_pairs, detail_computed, detail_reused = (
        _refine_coarse_groups(
            output_dir,
            store,
            all_features,
            coarse_groups,
            detail_threshold=params.detail_threshold,
            min_cluster_size=params.min_cluster_size,
            force=params.force,
        )
    )
    clusters: list[Cluster] = clusters_from_groups(
        refined_groups,
        all_features,
        params.min_cluster_size,
    )

    params_dict: dict[str, object] = {
        "threshold": params.threshold,
        "dhash_threshold": params.dhash_threshold,
        "color_threshold": params.color_threshold,
        "detail_threshold": params.detail_threshold,
        "min_cluster_size": params.min_cluster_size,
        "lsh_bands": params.lsh_bands,
        "force": params.force,
        "hash_algorithm": IMAGE_HASH_ALGORITHM,
        "detail_score_algorithm": DETAIL_SCORE_ALGORITHM,
        "linkage": "complete",
    }
    run_id = store.save_run(params_dict, clusters)

    clustered_images = sum(len(c.members) for c in clusters)

    return ClusterRunResult(
        run_id=run_id,
        total_images=total_images,
        features_computed=computed,
        features_reused=reused,
        features_deleted=len(deleted_paths),
        candidate_pairs=len(pairs),
        coarse_cluster_count=coarse_cluster_count,
        detail_pairs=detail_pairs,
        detail_scores_computed=detail_computed,
        detail_scores_reused=detail_reused,
        cluster_count=len(clusters),
        clustered_images=clustered_images,
    )
