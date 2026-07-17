from __future__ import annotations

from pathlib import Path
from typing import TypedDict
from urllib.parse import quote

from nga_tools.image_cluster.cluster import Cluster, ClusterMember
from nga_tools.image_cluster.store import ImageClusterStore


class ClusterMemberItem(TypedDict):
    relativePath: str
    fileUrl: str
    isSourceCandidate: bool


class ClusterListItem(TypedDict):
    clusterId: int
    memberCount: int
    sourceRelativePath: str
    sourceFileUrl: str


class ClusterDetailItem(TypedDict):
    clusterId: int
    members: list[ClusterMemberItem]


class ClustersResult(TypedDict):
    runId: int | None
    items: list[ClusterListItem]
    total: int
    offset: int
    limit: int


class ClusterDetailResult(TypedDict):
    runId: int | None
    cluster: ClusterDetailItem | None


class ClusterStatsResult(TypedDict):
    runId: int | None
    totalClusters: int
    totalImages: int
    maxClusterSize: int


def _member_item(member: ClusterMember) -> ClusterMemberItem:
    return {
        "relativePath": member.relative_path,
        "fileUrl": "/api/files/" + quote(member.relative_path, safe="/"),
        "isSourceCandidate": member.is_source_candidate,
    }


def _source_path(cluster: Cluster) -> str:
    for member in cluster.members:
        if member.is_source_candidate:
            return member.relative_path
    return cluster.members[0].relative_path if cluster.members else ""


def _file_url(relative_path: str) -> str:
    return "/api/files/" + quote(relative_path, safe="/")


def read_clusters(
    output_dir: Path,
    run_id: int | None,
    min_size: int,
    offset: int,
    limit: int,
) -> ClustersResult:
    store = ImageClusterStore(output_dir)
    effective_run_id = run_id if run_id is not None else store.latest_run_id()
    if effective_run_id is None:
        return ClustersResult(
            runId=None,
            items=[],
            total=0,
            offset=offset,
            limit=limit,
        )

    clusters = store.load_clusters(effective_run_id)
    filtered = [c for c in clusters if len(c.members) >= min_size]
    total = len(filtered)
    page = filtered[offset : offset + limit] if limit > 0 else filtered

    items: list[ClusterListItem] = [
        {
            "clusterId": c.cluster_id,
            "memberCount": len(c.members),
            "sourceRelativePath": _source_path(c),
            "sourceFileUrl": _file_url(_source_path(c)),
        }
        for c in page
    ]
    return ClustersResult(
        runId=effective_run_id,
        items=items,
        total=total,
        offset=offset,
        limit=limit,
    )


def read_cluster_detail(
    output_dir: Path, run_id: int | None, cluster_id: int
) -> ClusterDetailResult:
    store = ImageClusterStore(output_dir)
    effective_run_id = run_id if run_id is not None else store.latest_run_id()
    if effective_run_id is None:
        return ClusterDetailResult(runId=None, cluster=None)

    clusters = store.load_clusters(effective_run_id)
    target = next((c for c in clusters if c.cluster_id == cluster_id), None)
    if target is None:
        return ClusterDetailResult(runId=effective_run_id, cluster=None)

    return ClusterDetailResult(
        runId=effective_run_id,
        cluster={
            "clusterId": target.cluster_id,
            "members": [_member_item(m) for m in target.members],
        },
    )


def read_cluster_stats(
    output_dir: Path, run_id: int | None
) -> ClusterStatsResult:
    store = ImageClusterStore(output_dir)
    effective_run_id = run_id if run_id is not None else store.latest_run_id()
    if effective_run_id is None:
        return ClusterStatsResult(
            runId=None,
            totalClusters=0,
            totalImages=0,
            maxClusterSize=0,
        )

    clusters = store.load_clusters(effective_run_id)
    sizes = [len(c.members) for c in clusters]
    return ClusterStatsResult(
        runId=effective_run_id,
        totalClusters=len(clusters),
        totalImages=sum(sizes),
        maxClusterSize=max(sizes) if sizes else 0,
    )
