"""核心领域逻辑：训练件登记、同页判定、清单发布与冲突物化。

不变量：
1. scan_components 始终等于 active 同页边的连通分量（传递闭包），
   按清单号查询时无需重新串联同页判定。
2. conflicts 始终精确等于「清单内评测件所在分量含训练件」的全部
   (清单号, 评测件号, 训练件号) 三元组。
3. 清单供数状态由 conflicts 派生：无冲突 -> 按原编号供出原清单；
   有冲突 -> 暂停供数，凭清单号只返回冲突的评测件号与训练件号。

抽页方案（多批清单共用人工转写原文）：
- 原页 = 同页连通分量；共用页 = 同时含至少两批清单件号的分量。
- 选页是最小连通超边问题：以「页覆盖的批次」为超边，求能把全部批次
  连为一个连通组的最少超边；共用页上限 14，穷举规模可控。
- 方案在单事务内整份落库后才发编号；件号按 (方案, 页, 清单号) 固化，
  日后凭编号查到的始终是当初选中的件号，并对当前已提交资料复核可用性。

每个公开写操作都必须包在 db.write_tx 单事务内调用：
登记/判定变动与供数结论一并生效，任一步失败整体回滚，沿用原状态。
"""

from __future__ import annotations

import itertools
import sqlite3
import uuid
from datetime import datetime, timezone


class DomainError(Exception):
    """领域错误，status_code 映射为 HTTP 状态码。"""

    status_code = 400

    def __init__(self, message: str = "", **extra):
        super().__init__(message or self.__class__.__name__)
        self.extra = extra


class NotFound(DomainError):
    status_code = 404


class EvidenceMismatch(DomainError):
    status_code = 409


class ManifestConflict(DomainError):
    status_code = 409

    def __init__(self, conflicts: list[dict]):
        super().__init__("清单中存在曾用于训练或与训练件同页的件号，已退回", conflicts=conflicts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 连通分量（增量维护）
# ---------------------------------------------------------------------------

def _component_of(conn: sqlite3.Connection, scan_id: str) -> int | None:
    row = conn.execute(
        "SELECT component_id FROM scan_components WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    return None if row is None else row["component_id"]


def _ensure_component(conn: sqlite3.Connection, scan_id: str) -> int:
    comp = _component_of(conn, scan_id)
    if comp is not None:
        return comp
    comp = conn.execute("INSERT INTO components DEFAULT VALUES").lastrowid
    conn.execute(
        "INSERT INTO scan_components (scan_id, component_id) VALUES (?, ?)",
        (scan_id, comp),
    )
    return comp


def _component_members(conn: sqlite3.Connection, component_id: int) -> list[str]:
    return [
        r["scan_id"]
        for r in conn.execute(
            "SELECT scan_id FROM scan_components WHERE component_id = ?",
            (component_id,),
        )
    ]


def _merge_components(conn: sqlite3.Connection, c1: int, c2: int) -> int:
    """合并两个分量，返回保留的分量号。"""
    keep, drop = min(c1, c2), max(c1, c2)
    conn.execute(
        "UPDATE scan_components SET component_id = ? WHERE component_id = ?",
        (keep, drop),
    )
    conn.execute("DELETE FROM components WHERE component_id = ?", (drop,))
    return keep


def _split_after_withdrawal(
    conn: sqlite3.Connection, a: str, b: str, members: list[str]
) -> bool:
    """撤回边 (a, b) 后检查原分量是否分裂。

    仅在原分量成员范围内做 BFS；若 a 到 b 不再连通，把 a 所在侧迁往新分量。
    返回是否发生了分裂。
    """
    placeholders = ",".join("?" * len(members))
    edges = conn.execute(
        f"SELECT scan_a, scan_b FROM same_page_edges "
        f"WHERE active = 1 AND scan_a IN ({placeholders}) AND scan_b IN ({placeholders})",
        (*members, *members),
    ).fetchall()
    adjacency: dict[str, list[str]] = {}
    for e in edges:
        adjacency.setdefault(e["scan_a"], []).append(e["scan_b"])
        adjacency.setdefault(e["scan_b"], []).append(e["scan_a"])

    seen = {a}
    stack = [a]
    while stack:
        x = stack.pop()
        for y in adjacency.get(x, ()):
            if y not in seen:
                seen.add(y)
                stack.append(y)
    if b in seen:
        return False  # 仍有其他路径相连，分量不变

    new_comp = conn.execute("INSERT INTO components DEFAULT VALUES").lastrowid
    conn.executemany(
        "UPDATE scan_components SET component_id = ? WHERE scan_id = ?",
        [(new_comp, s) for s in seen],
    )
    return True


# ---------------------------------------------------------------------------
# 冲突物化
# ---------------------------------------------------------------------------

def _trainings_in_component(conn: sqlite3.Connection, component_id: int) -> list[str]:
    return [
        r["scan_id"]
        for r in conn.execute(
            "SELECT t.scan_id FROM training_scans t "
            "JOIN scan_components c ON c.scan_id = t.scan_id "
            "WHERE c.component_id = ? ORDER BY t.scan_id",
            (component_id,),
        )
    ]


def _recompute_conflicts(conn: sqlite3.Connection, scan_ids: list[str]) -> None:
    """对受影响的评测件号重建其全部冲突对（先删后插，幂等）。"""
    scan_ids = list(dict.fromkeys(scan_ids))
    if not scan_ids:
        return
    conn.executemany(
        "DELETE FROM conflicts WHERE eval_scan_id = ?", [(s,) for s in scan_ids]
    )
    for scan_id in scan_ids:
        comp = _component_of(conn, scan_id)
        if comp is None:
            continue
        trainings = _trainings_in_component(conn, comp)
        if not trainings:
            continue
        manifest_ids = [
            r["manifest_id"]
            for r in conn.execute(
                "SELECT manifest_id FROM manifest_items WHERE scan_id = ?",
                (scan_id,),
            )
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO conflicts (manifest_id, eval_scan_id, training_scan_id)"
            " VALUES (?, ?, ?)",
            [(m, scan_id, t) for m in manifest_ids for t in trainings],
        )


# ---------------------------------------------------------------------------
# 训练件登记
# ---------------------------------------------------------------------------

def register_training_scan(conn: sqlite3.Connection, scan_id: str) -> bool:
    """登记曾用于训练的扫描件号。返回是否新登记（重复登记幂等）。"""
    comp = _ensure_component(conn, scan_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO training_scans (scan_id, created_at) VALUES (?, ?)",
        (scan_id, _now()),
    )
    created = cur.rowcount > 0
    if created:
        # 该分量内所有评测件都可能新增与它的冲突
        _recompute_conflicts(conn, _component_members(conn, comp))
    return created


def deregister_training_scan(conn: sqlite3.Connection, scan_id: str) -> None:
    """注销训练件登记（冲突随之消除）。"""
    comp = _component_of(conn, scan_id)
    cur = conn.execute("DELETE FROM training_scans WHERE scan_id = ?", (scan_id,))
    if cur.rowcount == 0:
        raise NotFound(f"训练件 {scan_id} 未登记")
    if comp is not None:
        _recompute_conflicts(conn, _component_members(conn, comp))


def list_training_scans(conn: sqlite3.Connection) -> list[str]:
    return [
        r["scan_id"]
        for r in conn.execute("SELECT scan_id FROM training_scans ORDER BY scan_id")
    ]


# ---------------------------------------------------------------------------
# 同页判定（按证据号登记 / 撤回）
# ---------------------------------------------------------------------------

def register_same_page(
    conn: sqlite3.Connection, evidence_id: str, scan_a: str, scan_b: str
) -> bool:
    """按证据号登记两件同页的判定。间接相连（传递）也算同页。

    返回是否发生了状态变化（重复登记同一有效判定为幂等）。
    已撤回的同一证据号可重新登记；证据号不得改指其他件号对。
    """
    row = conn.execute(
        "SELECT scan_a, scan_b, active FROM same_page_edges WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if row is not None:
        if {row["scan_a"], row["scan_b"]} != {scan_a, scan_b}:
            raise EvidenceMismatch(f"证据号 {evidence_id} 已用于其他件号对")
        if row["active"]:
            return False
        conn.execute(
            "UPDATE same_page_edges SET active = 1, withdrawn_at = NULL"
            " WHERE evidence_id = ?",
            (evidence_id,),
        )
    else:
        conn.execute(
            "INSERT INTO same_page_edges (evidence_id, scan_a, scan_b, active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (evidence_id, scan_a, scan_b, _now()),
        )

    comp_a = _ensure_component(conn, scan_a)
    comp_b = _ensure_component(conn, scan_b)
    if comp_a != comp_b:
        keep = _merge_components(conn, comp_a, comp_b)
        _recompute_conflicts(conn, _component_members(conn, keep))
    return True


def withdraw_same_page(conn: sqlite3.Connection, evidence_id: str) -> bool:
    """按证据号撤回同页判定。返回是否发生了状态变化（重复撤回幂等）。"""
    row = conn.execute(
        "SELECT scan_a, scan_b, active FROM same_page_edges WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"证据号 {evidence_id} 不存在")
    if not row["active"]:
        return False
    conn.execute(
        "UPDATE same_page_edges SET active = 0, withdrawn_at = ? WHERE evidence_id = ?",
        (_now(), evidence_id),
    )
    comp = _component_of(conn, row["scan_a"])
    if comp is not None:
        members = _component_members(conn, comp)
        if _split_after_withdrawal(conn, row["scan_a"], row["scan_b"], members):
            # 分量分裂后，原分量两侧的评测件冲突都可能变化
            _recompute_conflicts(conn, members)
    return True


def list_same_page_links(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT evidence_id, scan_a, scan_b, active, created_at, withdrawn_at"
            " FROM same_page_edges ORDER BY evidence_id"
        )
    ]


# ---------------------------------------------------------------------------
# 评测清单
# ---------------------------------------------------------------------------

def _new_manifest_id(conn: sqlite3.Connection) -> str:
    while True:
        manifest_id = "MFT-" + uuid.uuid4().hex[:12].upper()
        exists = conn.execute(
            "SELECT 1 FROM manifests WHERE manifest_id = ?", (manifest_id,)
        ).fetchone()
        if exists is None:
            return manifest_id


def submit_manifest(conn: sqlite3.Connection, items: list[str]) -> tuple[str, list[str]]:
    """提交评测件号清单。

    任一件曾用于训练或与训练件（直接或间接）同页 -> 抛出 ManifestConflict，
    事务回滚，清单不发布。均无冲突才发布并返回清单号。
    """
    items = list(dict.fromkeys(items))  # 去重保序
    conflicts: list[dict] = []
    for scan_id in items:
        comp = _component_of(conn, scan_id)
        if comp is None:
            continue  # 从未出现过的件号：无训练登记、无同页判定，必无冲突
        for training_id in _trainings_in_component(conn, comp):
            conflicts.append(
                {"eval_scan_id": scan_id, "training_scan_id": training_id}
            )
    if conflicts:
        raise ManifestConflict(conflicts)

    manifest_id = _new_manifest_id(conn)
    conn.execute(
        "INSERT INTO manifests (manifest_id, created_at) VALUES (?, ?)",
        (manifest_id, _now()),
    )
    for position, scan_id in enumerate(items):
        conn.execute(
            "INSERT INTO manifest_items (manifest_id, scan_id, position)"
            " VALUES (?, ?, ?)",
            (manifest_id, scan_id, position),
        )
        # 让评测件进入分量表，后续训练登记/同页判定变动才能定位到它
        _ensure_component(conn, scan_id)
    return manifest_id, items


def get_manifest(conn: sqlite3.Connection, manifest_id: str) -> dict | None:
    """凭清单号查询。只读 SELECT，无需重新串联同页判定。

    无冲突：供出原清单；有冲突：暂停供数，只返回冲突的评测件号与训练件号。
    """
    manifest = conn.execute(
        "SELECT manifest_id FROM manifests WHERE manifest_id = ?", (manifest_id,)
    ).fetchone()
    if manifest is None:
        return None
    conflicts = conn.execute(
        "SELECT eval_scan_id, training_scan_id FROM conflicts"
        " WHERE manifest_id = ? ORDER BY eval_scan_id, training_scan_id",
        (manifest_id,),
    ).fetchall()
    if conflicts:
        return {
            "manifest_id": manifest_id,
            "status": "suspended",
            "conflicts": [dict(r) for r in conflicts],
        }
    items = conn.execute(
        "SELECT scan_id FROM manifest_items WHERE manifest_id = ? ORDER BY position",
        (manifest_id,),
    ).fetchall()
    return {
        "manifest_id": manifest_id,
        "status": "published",
        "items": [r["scan_id"] for r in items],
    }


# ---------------------------------------------------------------------------
# 抽页方案：多批清单共用人工转写原文
# ---------------------------------------------------------------------------

# 共用原页数量上限：超出即要求缩小范围，同时也把穷举规模限制在可控范围
SHARED_PAGE_LIMIT = 14


class SamplingInfeasible(DomainError):
    """共用页无法把所有批次连通，返回各自连通的批次组。"""

    status_code = 409

    def __init__(self, groups: list[list[str]]):
        super().__init__("共用页无法连通全部清单批次，请调整清单范围", groups=groups)


class SamplingRangeTooLarge(DomainError):
    """共用原页超过上限，需缩小范围。"""

    status_code = 400

    def __init__(self, count: int, limit: int):
        super().__init__(
            f"在至少两批出现的原页有 {count} 页，超过 {limit} 页上限，请缩小范围",
            shared_page_count=count,
            limit=limit,
        )


class SuspendedManifests(DomainError):
    """入参中存在暂停供数的清单，抽页只接受正常供数的清单。"""

    status_code = 409

    def __init__(self, manifest_ids: list[str]):
        super().__init__(
            "存在暂停供数的清单，恢复供数后再发起抽页",
            suspended_manifest_ids=manifest_ids,
        )


class UnknownManifests(DomainError):
    status_code = 404

    def __init__(self, manifest_ids: list[str]):
        super().__init__("部分清单号不存在", missing_manifest_ids=manifest_ids)


def _is_manifest_suspended(conn: sqlite3.Connection, manifest_id: str) -> bool:
    """供数状态由 conflicts 表派生：有任一冲突行即暂停供数。"""
    return (
        conn.execute(
            "SELECT 1 FROM conflicts WHERE manifest_id = ? LIMIT 1", (manifest_id,)
        ).fetchone()
        is not None
    )


def _manifest_components(
    conn: sqlite3.Connection, manifest_id: str
) -> list[int]:
    """清单内各件号当前所属的同页连通分量（即原页），去重排序。"""
    rows = conn.execute(
        "SELECT DISTINCT c.component_id FROM manifest_items m "
        "JOIN scan_components c ON c.scan_id = m.scan_id "
        "WHERE m.manifest_id = ? ORDER BY c.component_id",
        (manifest_id,),
    ).fetchall()
    return [r["component_id"] for r in rows]


def _connected_groups(
    manifest_ids: list[str], page_to_manifests: dict[int, set[str]]
) -> list[list[str]]:
    """以共用页为超边，求批次的连通组；组与组内清单均按入参顺序输出。"""
    parent = {m: m for m in manifest_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for members in page_to_manifests.values():
        members = [m for m in manifest_ids if m in members]
        for other in members[1:]:
            union(members[0], other)

    groups: dict[str, list[str]] = {}
    for m in manifest_ids:
        groups.setdefault(find(m), []).append(m)
    return list(groups.values())


def _minimum_connecting_pages(
    manifest_ids: list[str], shared_pages: list[int],
    page_to_manifests: dict[int, set[str]],
) -> list[int]:
    """求能把全部批次连为一组的最少原页（最小连通超边集）。

    按选用页数从小到大穷举；同规模按 shared_pages 中的次序取字典序最小者，
    保证方案确定。共用页上限 14，最坏情况组合数仍可接受。
    """
    n = len(manifest_ids)
    index_of = {m: i for i, m in enumerate(manifest_ids)}
    # 预计算每页覆盖的批次位掩码，连通判定用并查集
    masks: list[int] = []
    for comp in shared_pages:
        mask = 0
        for m in page_to_manifests[comp]:
            mask |= 1 << index_of[m]
        masks.append(mask)

    def connects(chosen: tuple[int, ...]) -> bool:
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        seen_groups = 0
        seen_mask = 0
        for idx in chosen:
            mask = masks[idx]
            seen_mask |= mask
            bits = [i for i in range(n) if mask & (1 << i)]
            for b in bits[1:]:
                ra, rb = find(bits[0]), find(b)
                if ra != rb:
                    parent[rb] = ra
        # 所有批次都被某张选中页覆盖，且并查集只有一个根
        if seen_mask != (1 << n) - 1:
            return False
        roots = {find(i) for i in range(n)}
        return len(roots) == 1

    for size in range(1, len(shared_pages) + 1):
        for combo in itertools.combinations(range(len(shared_pages)), size):
            if connects(combo):
                return [shared_pages[i] for i in combo]
    return []  # 无法连通，由调用方按连通组处理


def _new_plan_id(conn: sqlite3.Connection) -> str:
    while True:
        plan_id = "PLN-" + uuid.uuid4().hex[:12].upper()
        exists = conn.execute(
            "SELECT 1 FROM sampling_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if exists is None:
            return plan_id


def create_sampling_plan(
    conn: sqlite3.Connection, manifest_ids: list[str]
) -> dict:
    """为 2~6 个不同且正常供数的清单生成抽页方案（须包在写事务内调用）。

    流程：
    1. 校验清单号均存在且正常供数；
    2. 以同页连通分量为原页，找出在至少两批出现的共用页；
       超过 SHARED_PAGE_LIMIT 页 -> SamplingRangeTooLarge；
    3. 选最少的原页把任意两批直接或间接连起来；无法连通 ->
       SamplingInfeasible（附各自连通的批次组）；
    4. 选出的每页在含它的各批各取一件，整份方案落库后返回编号。
    """
    # 1. 入参校验（去重与数量约束在路由层由 pydantic 保证，这里防御性复核）
    manifest_ids = list(dict.fromkeys(manifest_ids))
    if not 2 <= len(manifest_ids) <= 6:
        raise DomainError("清单号数量必须在 2 至 6 个之间")

    existing = {
        r["manifest_id"]
        for r in conn.execute(
            f"SELECT manifest_id FROM manifests WHERE manifest_id IN "
            f"({','.join('?' * len(manifest_ids))})",
            manifest_ids,
        )
    }
    missing = [m for m in manifest_ids if m not in existing]
    if missing:
        raise UnknownManifests(missing)

    suspended = [m for m in manifest_ids if _is_manifest_suspended(conn, m)]
    if suspended:
        raise SuspendedManifests(suspended)

    # 2. 统计原页（分量）-> 含该页件号的批次
    page_to_manifests: dict[int, set[str]] = {}
    for manifest_id in manifest_ids:
        for comp in _manifest_components(conn, manifest_id):
            page_to_manifests.setdefault(comp, set()).add(manifest_id)

    shared_pages = sorted(
        comp for comp, ms in page_to_manifests.items() if len(ms) >= 2
    )
    if len(shared_pages) > SHARED_PAGE_LIMIT:
        raise SamplingRangeTooLarge(len(shared_pages), SHARED_PAGE_LIMIT)

    # 3. 最少选页连通全部批次
    chosen = _minimum_connecting_pages(
        manifest_ids, shared_pages, page_to_manifests
    )
    if not chosen:
        raise SamplingInfeasible(
            _connected_groups(manifest_ids, page_to_manifests)
        )

    # 4. 每页在含它的各批各取一件（取件号最小者，保证确定可复现），
    #    整份方案在同一事务内落库后才发编号
    plan_id = _new_plan_id(conn)
    conn.execute(
        "INSERT INTO sampling_plans (plan_id, created_at) VALUES (?, ?)",
        (plan_id, _now()),
    )
    for position, manifest_id in enumerate(manifest_ids):
        conn.execute(
            "INSERT INTO sampling_plan_manifests (plan_id, position, manifest_id)"
            " VALUES (?, ?, ?)",
            (plan_id, position, manifest_id),
        )

    pages_out: list[dict] = []
    for page_index, comp in enumerate(chosen):
        conn.execute(
            "INSERT INTO sampling_plan_pages (plan_id, page_index, component_id)"
            " VALUES (?, ?, ?)",
            (plan_id, page_index, comp),
        )
        members = [m for m in manifest_ids if m in page_to_manifests[comp]]
        picks: list[dict] = []
        for manifest_id in members:
            row = conn.execute(
                "SELECT m.scan_id FROM manifest_items m "
                "JOIN scan_components c ON c.scan_id = m.scan_id "
                "WHERE m.manifest_id = ? AND c.component_id = ? "
                "ORDER BY m.scan_id LIMIT 1",
                (manifest_id, comp),
            ).fetchone()
            scan_id = row["scan_id"]
            conn.execute(
                "INSERT INTO sampling_plan_picks (plan_id, page_index, manifest_id, scan_id)"
                " VALUES (?, ?, ?, ?)",
                (plan_id, page_index, manifest_id, scan_id),
            )
            picks.append({"manifest_id": manifest_id, "scan_id": scan_id})
        pages_out.append(
            {
                "page_index": page_index,
                "component_id": comp,
                "picks": picks,
            }
        )

    return {
        "plan_id": plan_id,
        "status": "ready",
        "manifest_ids": manifest_ids,
        "pages": pages_out,
    }


def get_sampling_plan(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    """凭方案编号查询（只读，以当前一份已提交资料为准）。

    始终能查到当初固化的清单号与件号；同时复核当前资料，任一情形则把原方案
    标为不可用（status=unavailable）并给出原因：
    - manifest_suspended：来源清单暂停供数；
    - picks_page_split：同组（同页同批）固化件号现已分属不同原页；
    - pages_merged：方案中两组合到了同一原页。
    """
    plan = conn.execute(
        "SELECT plan_id, created_at FROM sampling_plans WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if plan is None:
        return None

    manifest_rows = conn.execute(
        "SELECT manifest_id FROM sampling_plan_manifests"
        " WHERE plan_id = ? ORDER BY position",
        (plan_id,),
    ).fetchall()
    manifest_ids = [r["manifest_id"] for r in manifest_rows]

    page_rows = conn.execute(
        "SELECT page_index, component_id FROM sampling_plan_pages"
        " WHERE plan_id = ? ORDER BY page_index",
        (plan_id,),
    ).fetchall()
    pick_rows = conn.execute(
        "SELECT p.page_index, p.manifest_id, p.scan_id"
        " FROM sampling_plan_picks p"
        " JOIN sampling_plan_manifests m"
        " ON m.plan_id = p.plan_id AND m.manifest_id = p.manifest_id"
        " WHERE p.plan_id = ? ORDER BY p.page_index, m.position",
        (plan_id,),
    ).fetchall()

    pages: list[dict] = []
    picks_by_page: dict[int, list[dict]] = {}
    for r in pick_rows:
        pick = {"manifest_id": r["manifest_id"], "scan_id": r["scan_id"]}
        picks_by_page.setdefault(r["page_index"], []).append(pick)
    for r in page_rows:
        pages.append(
            {
                "page_index": r["page_index"],
                "component_id": r["component_id"],
                "picks": picks_by_page.get(r["page_index"], []),
            }
        )

    reasons: list[dict] = []

    # 原因 1：来源清单暂停供数
    suspended = [m for m in manifest_ids if _is_manifest_suspended(conn, m)]
    if suspended:
        reasons.append(
            {"code": "manifest_suspended", "manifest_ids": suspended}
        )

    # 原因 2：同组件号分属不同页（以当前同页分量为准复核每个固化件号）
    current_components: dict[str, int | None] = {}
    for r in pick_rows:
        if r["scan_id"] not in current_components:
            current_components[r["scan_id"]] = _component_of(
                conn, r["scan_id"]
            )
    for page in pages:
        comps = {
            current_components.get(p["scan_id"]) for p in page["picks"]
        }
        comps.discard(None)
        if len(comps) >= 2:
            reasons.append(
                {"code": "picks_page_split", "page_index": page["page_index"]}
            )

    # 原因 3：两组合到同一页（方案中不同页的固化件号现属同一分量）
    page_components_now: dict[int, set[int]] = {}
    for page in pages:
        comps = {
            current_components.get(p["scan_id"]) for p in page["picks"]
        }
        comps.discard(None)
        page_components_now[page["page_index"]] = comps
    for earlier, later in itertools.combinations(
        [p["page_index"] for p in pages], 2
    ):
        if page_components_now[earlier] & page_components_now[later]:
            reasons.append(
                {
                    "code": "pages_merged",
                    "page_indices": [earlier, later],
                }
            )

    return {
        "plan_id": plan_id,
        "status": "unavailable" if reasons else "ready",
        "manifest_ids": manifest_ids,
        "pages": pages,
        **({"unavailable_reasons": reasons} if reasons else {}),
    }
