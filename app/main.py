"""港口潮汐通航行程规划服务 —— FastAPI 入口。"""
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import config
from .database import get_conn, init_db
from .errors import ApiError
from .schemas import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    BatchPlanRequest,
    BatchPlanResponse,
    BerthCalendarReplaceRequest,
    BerthCreateRequest,
    BerthOut,
    BillOut,
    BillResponse,
    ChannelGroupCreateRequest,
    ChannelGroupOut,
    ConfirmRequest,
    NodeIn,
    PlanRequest,
    PlanResponse,
    RescheduleConfirmOut,
    RescheduleConfirmRequest,
    ReversalOut,
    ReviewRequest,
    RevisionOut,
    SegmentIn,
    SegmentOut,
    ShipCreateRequest,
    ShipOut,
    ShipUpdateRequest,
    ShipUpdateResponse,
    ShipVersionOut,
    TollRateReplaceRequest,
    TollRateVersionOut,
    TravelControlListOut,
    TravelControlOut,
    TravelControlReplaceRequest,
    TideCorrection,
    TideErrorRevision,
    TripOut,
    TripReviewOut,
    TripVoidRequest,
    TripVoidResponse,
    VoidRecordOut,
)
from .services.batch import confirm_batch, plan_batch
from .services.berths import (
    create_berth,
    get_berth,
    list_berths,
    load_berth_occupancy,
    replace_calendar,
)
from .services.controls import load_controls, replace_segment_controls
from .services.groups import create_group, get_group, list_groups
from .services.planning import find_itinerary, load_occupancy
from .services.reschedule import review_and_reschedule
from .services.reschedule_confirm import confirm_reschedule
from .services.ships import (
    create_ship,
    get_ship,
    get_version,
    list_ships,
    list_versions,
    resolve_for_confirm,
    resolve_for_plan,
    update_ship,
)
from .services.tide import (
    create_correction_revision,
    create_error_revision,
    load_tide_range,
)
from .services.toll import (
    bill_out,
    get_rate_version,
    get_reversal,
    issue_bill,
    list_bills,
    list_rate_versions,
    replace_segment_rates,
)
from .services.voids import get_void_record, void_trip


@asynccontextmanager
async def lifespan(app: FastAPI):
    with get_conn() as conn:
        init_db(conn)
    yield


app = FastAPI(
    title="港口潮汐通航行程规划服务",
    version="1.0.0",
    description=(
        "录入港口节点、单向航段与逐分钟潮位（带修订号），根据船舶吃水 / "
        "水面以上高度 / 安全余量计算受潮汐与桥高约束的最早抵达行程。"
    ),
    lifespan=lifespan,
)


# ---- 统一错误响应 ----------------------------------------------------------------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "details": {"errors": _safe_validation_errors(exc)},
            }
        },
    )


def _safe_validation_errors(exc: RequestValidationError) -> list:
    """Pydantic 错误清单的可 JSON 化形式。

    模型级校验（如船号与三项参数互斥）抛出的 ValueError 会出现在 ctx 中，
    不可 JSON 序列化，此处转为字符串；其余可序列化的约束上下文原样保留。
    """
    safe = []
    for err in exc.errors():
        err = dict(err)
        ctx = err.get("ctx")
        if ctx:
            err["ctx"] = {
                k: (str(v) if isinstance(v, Exception) else v)
                for k, v in ctx.items()
            }
        safe.append(err)
    return safe


@app.get("/health")
def health():
    return {"status": "ok"}


# ---- 节点 -----------------------------------------------------------------------

@app.post("/api/nodes", status_code=201, tags=["nodes"])
def create_node(body: NodeIn):
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO nodes(node_id, name) VALUES (?, ?)",
                (body.node_id, body.name),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "NODE_EXISTS", f"节点已存在: {body.node_id}",
                           {"node_id": body.node_id})
    return {"node_id": body.node_id, "name": body.name}


@app.get("/api/nodes", tags=["nodes"])
def list_nodes():
    with get_conn() as conn:
        rows = conn.execute("SELECT node_id, name FROM nodes ORDER BY node_id").fetchall()
    return [dict(r) for r in rows]


# ---- 泊位 -------------------------------------------------------------------------

@app.post("/api/nodes/{node_id}/berths",
          status_code=201, response_model=BerthOut, tags=["berths"])
def create_node_berth(node_id: str, body: BerthCreateRequest):
    """为现有节点登记泊位（泊位编号全局唯一），初始开放日历为空（全程不开放），
    随后须以 PUT /api/berths/{berth_id}/calendar 整组设置开放区间。

    未知节点 404 NODE_NOT_FOUND；泊位编号重复 409 BERTH_EXISTS。
    """
    with get_conn() as conn:
        return create_berth(conn, node_id, body.berth_id, body.note)


@app.get("/api/berths", response_model=list[BerthOut], tags=["berths"])
def list_all_berths(node: Optional[str] = None):
    """列出全部泊位（按泊位编号升序，各附当前开放日历）。

    可用 ?node=X 只看某节点的泊位；过滤未知节点 404 NODE_NOT_FOUND。
    """
    with get_conn() as conn:
        return list_berths(conn, node)


@app.get("/api/berths/{berth_id}", response_model=BerthOut, tags=["berths"])
def get_berth_view(berth_id: str):
    """读取泊位与其当前开放日历。未知泊位 404 BERTH_NOT_FOUND。"""
    with get_conn() as conn:
        return get_berth(conn, berth_id)


@app.put("/api/berths/{berth_id}/calendar",
         response_model=BerthOut, tags=["berths"])
def replace_berth_calendar(berth_id: str, body: BerthCalendarReplaceRequest):
    """以互不交叠的半开分钟区间 [start_minute, end_minute) 整组替换泊位开放
    日历，返回替换后的泊位视图。

    空组 intervals: [] 表示该泊位全程不开放。起点非负、终点大于起点，否则
    422 INVALID_BERTH_INTERVAL；组内半开交叠
    422 OVERLAPPING_BERTH_INTERVAL；未知泊位 404 BERTH_NOT_FOUND。任一失败
    旧日历原样保留。日历变更只影响之后的预排/确认复算——已确认快照固化的
    泊位作业区间不改变（历史占用仍按固化区间）。
    """
    with get_conn() as conn:
        out = replace_calendar(conn, berth_id,
                               [c.model_dump() for c in body.intervals])
    return out


# ---- 航段 -----------------------------------------------------------------------

@app.post("/api/segments", status_code=201, response_model=SegmentOut, tags=["segments"])
def create_segment(body: SegmentIn):
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (body.origin,)).fetchone():
            from .errors import unknown_node
            raise unknown_node(body.origin)
        if not conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (body.destination,)).fetchone():
            from .errors import unknown_node
            raise unknown_node(body.destination)
        cur = conn.execute(
            "INSERT INTO segments(origin, destination, travel_time, depth, clearance)"
            " VALUES (?, ?, ?, ?, ?)",
            (body.origin, body.destination, body.travel_time, body.depth, body.clearance),
        )
        seg_id = cur.lastrowid
    return SegmentOut(seg_id=seg_id, **body.model_dump())


@app.get("/api/segments", response_model=list[SegmentOut], tags=["segments"])
def list_segments():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT seg_id, origin, destination, travel_time, depth, clearance"
            " FROM segments ORDER BY seg_id"
        ).fetchall()
    return [dict(r) for r in rows]


# ---- 航段分时航时管制 -------------------------------------------------------------

def _control_list_out(conn, seg_id: int) -> TravelControlListOut:
    row = conn.execute(
        "SELECT seg_id, travel_time AS base_travel_time FROM segments"
        " WHERE seg_id = ?",
        (seg_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "SEGMENT_NOT_FOUND", f"未知航段: {seg_id}",
                       {"seg_id": seg_id})
    intervals = load_controls(conn, [seg_id]).get(seg_id, [])
    return TravelControlListOut(
        seg_id=seg_id,
        base_travel_time=row["base_travel_time"],
        controls=[
            TravelControlOut(start_minute=s, end_minute=e, travel_time=t)
            for s, e, t in intervals
        ],
    )


@app.put("/api/segments/{seg_id}/travel-controls",
         response_model=TravelControlListOut, tags=["segments"])
def replace_travel_controls(seg_id: int, body: TravelControlReplaceRequest):
    """以航段编号整组替换分时航时管制，返回替换后的生效清单。

    每个元素为半开分钟区间 [start_minute, end_minute) 及正整数通行分钟；
    船舶于进入分钟命中区间即按该通行分钟计航时（途中跨边界不变），未命中
    沿用航段基础航时。空组恢复基础航时。未知航段 404 SEGMENT_NOT_FOUND；
    非法区间 422 INVALID_CONTROL_INTERVAL；组内交叠
    422 OVERLAPPING_CONTROL_INTERVAL，任一失败均保留旧管制不变。
    """
    with get_conn() as conn:
        replace_segment_controls(
            conn, seg_id, [c.model_dump() for c in body.controls]
        )
        out = _control_list_out(conn, seg_id)
    return out


@app.get("/api/segments/{seg_id}/travel-controls",
         response_model=TravelControlListOut, tags=["segments"])
def get_travel_controls(seg_id: int):
    """读取航段当前生效的分时航时管制清单（按起点升序）。"""
    with get_conn() as conn:
        return _control_list_out(conn, seg_id)


# ---- 通行费费率版本 ----------------------------------------------------------------

@app.put("/api/segments/{seg_id}/toll-rates",
         status_code=201, response_model=TollRateVersionOut, tags=["toll"])
def replace_segment_toll_rates(seg_id: int, body: TollRateReplaceRequest):
    """财务按航段整组提交半开分钟费率区间，生成全局不可变费率新版本。

    每个元素为半开分钟区间 [start_minute, end_minute) 及**非负整数**费率
    （分/分钟）；新版本继承前一版本全部航段费率，仅整组替换该航段
    （空组表示该航段未覆盖分钟费率为 0）。开单按各段进入分钟选费率，
    未覆盖分钟费率为 0。未知航段 404 SEGMENT_NOT_FOUND；非法区间
    422 INVALID_TOLL_RATE_INTERVAL；组内交叠
    422 OVERLAPPING_TOLL_RATE_INTERVAL，任一失败均不生成新版本。
    """
    with get_conn() as conn:
        return replace_segment_rates(
            conn, seg_id, [r.model_dump() for r in body.rates], body.note
        )


@app.get("/api/toll/rate-versions",
         response_model=list[TollRateVersionOut], tags=["toll"])
def list_toll_rate_versions():
    """列出全部通行费费率版本（按版本号升序，各含其全部航段费率区间）。"""
    with get_conn() as conn:
        return list_rate_versions(conn)


@app.get("/api/toll/rate-versions/{version}",
         response_model=TollRateVersionOut, tags=["toll"])
def get_toll_rate_version(version: int):
    """读取指定通行费费率版本（不可变历史版本，含全部航段费率区间）。

    未知版本 404 TOLL_RATE_VERSION_NOT_FOUND。
    """
    with get_conn() as conn:
        return get_rate_version(conn, version)


# ---- 狭窄水道组 -------------------------------------------------------------------

@app.post("/api/channel-groups",
          status_code=201, response_model=ChannelGroupOut, tags=["channel-groups"])
def create_channel_group(body: ChannelGroupCreateRequest):
    """把至少两条现有航段登记为同一狭窄水道组，返回组号及成员。

    组建成后不可修改（无更新/解散接口），航段不得重复归组。船进入组内
    任一航段后，从进入分钟到抵达前一分钟独占整组。未知航段
    404 SEGMENT_NOT_FOUND、提交重复编号 422 DUPLICATE_SEGMENT_IN_GROUP、
    成员已归组 409 SEGMENT_ALREADY_GROUPED，任一失败均拒绝且不留半成品。
    """
    with get_conn() as conn:
        return create_group(conn, body.segments, body.note)


@app.get("/api/channel-groups",
         response_model=list[ChannelGroupOut], tags=["channel-groups"])
def list_channel_groups():
    """列出全部狭窄水道组（按组号升序，成员按提交次序）。"""
    with get_conn() as conn:
        return list_groups(conn)


@app.get("/api/channel-groups/{group_id}",
         response_model=ChannelGroupOut, tags=["channel-groups"])
def get_channel_group(group_id: int):
    """读取指定狭窄水道组的组号及成员。未知组 404 CHANNEL_GROUP_NOT_FOUND。"""
    with get_conn() as conn:
        return get_group(conn, group_id)


# ---- 船舶档案 -------------------------------------------------------------------

@app.post("/api/ships", status_code=201, response_model=ShipOut, tags=["ships"])
def create_ship_archive(body: ShipCreateRequest):
    """建立船舶档案：唯一船号 + 非负吃水/水面以上高度/安全余量 + 启停状态。

    初始版本为 1；重复船号 409 SHIP_EXISTS，非法参数 422 VALIDATION_ERROR，
    失败不留半成品。
    """
    with get_conn() as conn:
        return create_ship(conn, body.ship_no, body.draft, body.height,
                           body.margin, body.enabled, body.note)


@app.get("/api/ships", response_model=list[ShipOut], tags=["ships"])
def list_ship_archives():
    """列出全部船舶档案的当前版本（按船号升序）。"""
    with get_conn() as conn:
        return list_ships(conn)


@app.get("/api/ships/{ship_no}", response_model=ShipOut, tags=["ships"])
def get_ship_archive(ship_no: str):
    """读取指定船号的当前版本。未知船号 404 SHIP_NOT_FOUND。"""
    with get_conn() as conn:
        return get_ship(conn, ship_no)


@app.put("/api/ships/{ship_no}", response_model=ShipUpdateResponse, tags=["ships"])
def update_ship_archive(ship_no: str, body: ShipUpdateRequest):
    """变更船舶档案：未给出的字段沿用当前值。

    吃水/水面以上高度/安全余量/启停状态任一实际变化 -> 版本 +1
    （changed=true）；与当前完全一致（含空提交）-> 版本不变（changed=false）。
    历史版本不可变、可随时查询。未知船号 404 SHIP_NOT_FOUND。
    """
    with get_conn() as conn:
        state, changed = update_ship(conn, ship_no, body.draft, body.height,
                                     body.margin, body.enabled, body.note)
    return ShipUpdateResponse(**state, changed=changed)


@app.get("/api/ships/{ship_no}/versions",
         response_model=list[ShipVersionOut], tags=["ships"])
def list_ship_versions(ship_no: str):
    """列出指定船号的全部历史版本（按版本号升序）。未知船号 404。"""
    with get_conn() as conn:
        return list_versions(conn, ship_no)


@app.get("/api/ships/{ship_no}/versions/{version}",
         response_model=ShipVersionOut, tags=["ships"])
def get_ship_version(ship_no: str, version: int):
    """读取指定船号的某一历史版本。未知船号 SHIP_NOT_FOUND、未知版本
    SHIP_VERSION_NOT_FOUND。"""
    with get_conn() as conn:
        return get_version(conn, ship_no, version)


# ---- 潮位修订 --------------------------------------------------------------------

@app.get("/api/tide/revisions", response_model=list[RevisionOut], tags=["tide"])
def list_revisions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT revision, note, created_at FROM tide_revisions ORDER BY revision"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/tide/revisions/{revision}", tags=["tide"])
def get_revision(revision: int, from_minute: int = 0, to_minute: int = 1439):
    """取某修订逐分钟名义潮位与预测误差（分钟范围查询）。

    差量链式存储下沿基础修订链还原该修订在 [from_minute, to_minute] 的完整
    名义潮位与误差（见 services/tide.py）；未知修订 404 TIDE_REVISION_NOT_FOUND。
    """
    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (revision,)
        ).fetchone():
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           f"潮位修订不存在: {revision}", {"revision": revision})
        tide = load_tide_range(conn, revision, from_minute, to_minute)
    ordered = sorted(tide.items())
    return {"revision": revision, "from_minute": from_minute, "to_minute": to_minute,
            "count": len(ordered),
            "levels": {m: v[0] for m, v in ordered},
            "errors": {m: v[1] for m, v in ordered}}


@app.post("/api/tide/corrections", status_code=201, response_model=RevisionOut, tags=["tide"])
def apply_correction(body: TideCorrection):
    """名义潮位更正：基于某修订（可省略，省略取最新；允许从非最新修订分叉）
    生成新修订，只覆盖给定分钟的名义潮位（被覆盖分钟误差沿用基础修订，新
    分钟误差为 0），未列出分钟连同误差沿基础修订链继承。

    差量落库：本次修订只持久化改动分钟（services/tide.py），写入量随本次
    改动分钟数增长，不再复制基础修订全部分钟。
    """
    if not body.corrections:
        raise ApiError(422, "VALIDATION_ERROR", "corrections 不能为空")
    bad = [m for m in body.corrections if m < 0]
    if bad:
        raise ApiError(422, "VALIDATION_ERROR", "分钟必须为非负整数",
                       {"bad_minutes": bad})

    with get_conn() as conn:
        row = create_correction_revision(
            conn, body.corrections, body.base_revision, body.note
        )
    return RevisionOut(**row)


@app.post("/api/tide/error-revisions", status_code=201,
          response_model=RevisionOut, tags=["tide"])
def apply_error_revision(body: TideErrorRevision):
    """潮位预测误差修订：名义潮位不变，仅覆盖给定分钟的非负误差（米）。

    未列出分钟沿基础修订链继承误差；旧修订（error 列缺失/为 0）一律视为
    误差 0。所提交分钟在基础修订中必须有名义潮位（沿基础链还原判定），
    否则报 MISSING_TIDE_DATA。差量落库：只持久化本次改动分钟。
    """
    if not body.errors:
        raise ApiError(422, "VALIDATION_ERROR", "errors 不能为空")
    bad_minutes = [m for m in body.errors if m < 0]
    if bad_minutes:
        raise ApiError(422, "VALIDATION_ERROR", "分钟必须为非负整数",
                       {"bad_minutes": bad_minutes})
    bad_errors = [
        {"minute": m, "error": e} for m, e in sorted(body.errors.items()) if e < 0
    ]
    if bad_errors:
        raise ApiError(422, "VALIDATION_ERROR", "潮位误差必须为非负数（米）",
                       {"bad_errors": bad_errors})

    with get_conn() as conn:
        row = create_error_revision(
            conn, body.errors, body.base_revision, body.note
        )
    return RevisionOut(**row)


# ---- 行程规划 / 确认 --------------------------------------------------------------

@app.post("/api/trips/plan", response_model=PlanResponse, tags=["trips"])
def plan_trip(body: PlanRequest):
    """规划最早抵达行程（只读，不占位）。

    船舶参数两种输入二选一（混用拒绝）：直接提交 draft/height/margin，或
    提交 ship_no 按档案**当前启用版本**的参数规划（档案停用 409
    SHIP_DISABLED、未知船号 404 SHIP_NOT_FOUND）；响应回传实际所用参数，
    按档案规划时另回传船号与版本。
    """
    with get_conn() as conn:
        if body.ship_no is not None:
            params = resolve_for_plan(conn, body.ship_no)
        else:
            params = {"ship_no": None, "ship_version": None,
                      "draft": body.draft, "height": body.height,
                      "margin": body.margin}
        blocked = load_occupancy(
            conn, body.start_minute,
            body.start_minute + body.window_minutes - 1,
        )
        berth_blocked = load_berth_occupancy(
            conn, body.start_minute,
            body.start_minute + body.window_minutes - 1,
        )
        result = find_itinerary(
            conn,
            origin=body.origin,
            destination=body.destination,
            start_minute=body.start_minute,
            window_minutes=body.window_minutes,
            draft=params["draft"],
            height=params["height"],
            margin=params["margin"],
            revision=body.revision,
            blocked=blocked,
            berth_id=body.berth_id,
            work_minutes=body.work_minutes,
            berth_blocked=berth_blocked,
        )
        result["ship_no"] = params["ship_no"]
        result["ship_version"] = params["ship_version"]
    return result


@app.post("/api/trips/confirm", status_code=201, response_model=TripOut, tags=["trips"])
def confirm_trip(body: ConfirmRequest):
    """确认行程：必须携带所据修订号；同一事务内以最新潮位修订与当时占用
    重新计算，过期/不一致即冲突且不保存。

    按船号确认时须回填规划所得 ship_no 与 ship_version：事务内先核对档案
    仍启用且版本未变（停用 SHIP_DISABLED、版本已变 SHIP_VERSION_STALE、
    未知船号 SHIP_NOT_FOUND），再复算；快照固化所用参数与档案引用。
    """
    if body.revision is None:
        raise ApiError(422, "VALIDATION_ERROR", "确认行程必须携带所据潮位修订号 revision")

    with get_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 0) 档案核对（按船号确认时）：仍启用且版本未变，取档案当前参数
            if body.ship_no is not None:
                params = resolve_for_confirm(conn, body.ship_no, body.ship_version)
            else:
                params = {"ship_no": None, "ship_version": None,
                          "draft": body.draft, "height": body.height,
                          "margin": body.margin}

            # 1) 修订是否仍为最新（过期则冲突）
            latest = conn.execute(
                "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            if latest is None or latest["revision"] != body.revision:
                raise ApiError(
                    409,
                    "REVISION_STALE",
                    f"潮位已更新到修订 #{latest['revision'] if latest else None}，"
                    f"所据修订 #{body.revision} 已过期，请重新规划",
                    {"provided_revision": body.revision,
                     "current_revision": latest["revision"] if latest else None},
                )

            # 2) 同一事务内依据该修订与**当时占用**重新计算
            #    （既有已确认快照即使彼此重叠也一并作为占用；本行程尚未落库）
            blocked = load_occupancy(
                conn, body.start_minute, body.start_minute + body.window_minutes - 1
            )
            berth_blocked = load_berth_occupancy(
                conn, body.start_minute, body.start_minute + body.window_minutes - 1
            )
            try:
                recalculated = find_itinerary(
                    conn,
                    origin=body.origin,
                    destination=body.destination,
                    start_minute=body.start_minute,
                    window_minutes=body.window_minutes,
                    draft=params["draft"],
                    height=params["height"],
                    margin=params["margin"],
                    revision=body.revision,
                    blocked=blocked,
                    berth_id=body.berth_id,
                    work_minutes=body.work_minutes,
                    berth_blocked=berth_blocked,
                )
            except ApiError as exc:
                # 占用变化导致窗口内已无可行行程：提交行程不再符合最优结果
                if exc.code == "NO_FEASIBLE_ITINERARY":
                    raise ApiError(
                        409,
                        "PLAN_MISMATCH",
                        "占用已变化：窗口内已不存在可行行程（含终点泊位开放日历/"
                        "作业占用变化），提交行程不再符合最优结果，确认失败",
                        {"reason": "no_feasible_itinerary"},
                    )
                raise

            # 3) 回填行程必须与复核结果逐段一致
            def norm(legs):
                return [
                    (l["seg_id"], l["origin"], l["destination"],
                     l["start_minute"], l["end_minute"], l["travel_time"])
                    if isinstance(l, dict) else
                    (l.seg_id, l.origin, l.destination, l.start_minute, l.end_minute, l.travel_time)
                    for l in legs
                ]

            submitted = norm(body.itinerary)
            canonical = norm(recalculated["itinerary"])
            if submitted != canonical or recalculated["arrival_minute"] != _payload_arrival(body):
                raise ApiError(
                    409,
                    "PLAN_MISMATCH",
                    "行程复核结果与所提交行程不一致（窗口/船舶参数/潮位数据/"
                    "泊位开放日历可能已变化），确认失败",
                    {"recalculated": recalculated},
                )
            # 泊位与作业分钟亦须与复算一致（作业区间由抵达+作业分钟决定）
            if body.berth_id != recalculated["berth_id"] or \
                    body.work_minutes != recalculated["work_minutes"]:
                raise ApiError(
                    409,
                    "PLAN_MISMATCH",
                    "泊位/作业分钟复核结果与提交不一致，确认失败",
                    {"reason": "berth_mismatch", "recalculated": recalculated},
                )

            # 4) 复核通过：按"当时结果"保存不可变快照
            #    （固化所用参数与档案引用；指定泊位时固化泊位编号与作业区间，
            #      后续档案/日历变更不改写历史）
            cur = conn.execute(
                "INSERT INTO confirmed_trips"
                " (revision, origin, destination, start_minute, window_minutes,"
                "  draft, height, margin, arrival_minute, itinerary,"
                "  ship_no, ship_version, berth_id, work_minutes,"
                "  work_start_minute, work_end_minute)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (body.revision, body.origin, body.destination, body.start_minute,
                 body.window_minutes, params["draft"], params["height"],
                 params["margin"],
                 recalculated["arrival_minute"], json.dumps(recalculated["itinerary"]),
                 params["ship_no"], params["ship_version"],
                 recalculated["berth_id"], recalculated["work_minutes"],
                 recalculated["work_start_minute"],
                 recalculated["work_end_minute"]),
            )
            trip_id = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
            ).fetchone()
            conn.execute("COMMIT")
        except ApiError:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return _trip_out(dict(row))


def _payload_arrival(body: ConfirmRequest) -> Optional[int]:
    if not body.itinerary:
        return body.start_minute
    return body.itinerary[-1].end_minute


# ---- 批次预排 / 确认（同一修订下 2～5 艘船） ---------------------------------------

@app.post("/api/trips/batch/plan",
          response_model=BatchPlanResponse, tags=["trips", "batch"])
def plan_trip_batch(body: BatchPlanRequest):
    """批次预排（只读、不占位）。

    按 ships 给出的优先顺序逐艘沿用单船最早抵达 + 同刻航段序列字典序
    择优；后船同时避开全部已确认占用与本批前船的拟占用。任一船无解
    （或未知节点/缺潮位）时，错误 details 给出首个失败序号 ship_index
    （0 起）。revision 省略时整批采用最新修订。每艘船可用 ship_no 代替
    三项参数（按档案当前启用版本，混用拒绝），结果回传船号、版本及所用参数。
    """
    with get_conn() as conn:
        result = plan_batch(conn, body.ships, body.revision)
    return result


@app.post("/api/trips/batch/confirm",
          status_code=201, response_model=BatchConfirmResponse,
          tags=["trips", "batch"])
def confirm_trip_batch(body: BatchConfirmRequest):
    """批次确认：单事务内核订档案、最新修订与当时占用，逐艘复算并逐段比对。

    - 按船号提交的船：档案停用 409 SHIP_DISABLED、版本已变 409
      SHIP_VERSION_STALE、未知船号 404 SHIP_NOT_FOUND（均附 ship_index）；
    - 修订过期 -> 409 REVISION_STALE；
    - 任一船无解或与提交行程不一致 -> 409 PLAN_MISMATCH（details.ship_index
      指出首个失败序号），全批不保存；
    - 全部通过则一次写入全部不可变快照（固化所用参数与档案引用）并登记
      幂等键；同键同内容重试返回原结果（200，replayed=true，即使修订已
      更新）；同键异内容 409 BATCH_KEY_CONFLICT。
    """
    with get_conn() as conn:
        result, replayed = confirm_batch(conn, body)
    if replayed:
        return JSONResponse(status_code=200, content=result)
    return result


@app.get("/api/trips", response_model=list[TripOut], tags=["trips"])
def list_trips():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, v.voided_at AS voided_at FROM confirmed_trips c"
            " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
            " ORDER BY c.trip_id"
        ).fetchall()
    return [_trip_out(dict(r)) for r in rows]


@app.get("/api/trips/{trip_id}", response_model=TripOut, tags=["trips"])
def get_trip(trip_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.*, v.voided_at AS voided_at FROM confirmed_trips c"
            " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
            " WHERE c.trip_id = ?",
            (trip_id,),
        ).fetchone()
    if row is None:
        raise ApiError(404, "TRIP_NOT_FOUND", f"已确认行程不存在: {trip_id}",
                       {"trip_id": trip_id})
    return _trip_out(dict(row))


@app.post(
    "/api/trips/{trip_id}/void",
    response_model=TripVoidResponse,
    tags=["trips", "void"],
)
def void_confirmed_trip(trip_id: int, body: TripVoidRequest):
    """误确认后作废已确认行程（事务串行）。

    凭行程号与非空作废原因作废。作废不删除确认快照——已确认行程与原批次
    确认结果仍作为历史快照可查；仅该行程按固化航时计算的航段及狭窄水道
    组占用从后续调度（单船/批次的预排、确认复算、改期建议）中失效。
    重复作废返回**首次记录**（200，replayed=true），不覆盖原因/时间；
    未知行程 404 TRIP_NOT_FOUND。
    """
    reason = body.reason.strip()
    if not reason:
        raise ApiError(422, "VALIDATION_ERROR",
                       "作废原因不能为空（仅空白字符视为空）", {"field": "reason"})
    with get_conn() as conn:
        record, replayed = void_trip(conn, trip_id, reason)
    if replayed:
        return JSONResponse(status_code=200,
                            content={**record, "replayed": True})
    return JSONResponse(status_code=201,
                        content={**record, "replayed": False})


@app.get(
    "/api/trips/{trip_id}/void",
    response_model=VoidRecordOut,
    tags=["trips", "void"],
)
def get_trip_void_record(trip_id: int):
    """按行程号查询作废记录。

    未知行程（无确认快照）404 TRIP_NOT_FOUND；行程存在但未作废
    404 VOID_RECORD_NOT_FOUND；已作废返回行程号、首次原因与作废时间。
    """
    with get_conn() as conn:
        return get_void_record(conn, trip_id)


# ---- 通行费账单 / 冲销单 -------------------------------------------------------------

@app.post(
    "/api/trips/{trip_id}/bill",
    response_model=BillResponse,
    tags=["trips", "toll"],
)
def issue_trip_toll_bill(trip_id: int):
    """为已确认行程开具通行费账单（按当时最新费率版本固化）。

    同一事务内取当时最新费率版本，按确认快照各段**进入分钟**选费率，
    段金额 = 固化航时 × 费率（未覆盖分钟费率 0），账单总额为整数分。
    账单一经生成永不随后续费率版本改变。首次开单 201；重试（每行程只开
    一次）返回**原账单** 200、replayed=true。未知行程 404 TRIP_NOT_FOUND；
    已作废且未开单 409 TRIP_ALREADY_VOIDED；系统中尚无费率版本
    409 TOLL_RATE_VERSION_NOT_FOUND。
    """
    with get_conn() as conn:
        bill, replayed = issue_bill(conn, trip_id)
    if replayed:
        return JSONResponse(status_code=200, content={**bill, "replayed": True})
    return JSONResponse(status_code=201, content={**bill, "replayed": False})


@app.get(
    "/api/trips/{trip_id}/bill",
    response_model=BillOut,
    tags=["trips", "toll"],
)
def get_trip_toll_bill(trip_id: int):
    """按行程号查询通行费账单（含冲销单，若已生成）。

    未开单 404 BILL_NOT_FOUND；行程已作废时账单仍原样可查，其
    reversal 字段给出等额冲销单。
    """
    with get_conn() as conn:
        return bill_out(conn, trip_id)


@app.get(
    "/api/trips/{trip_id}/reversal",
    response_model=ReversalOut,
    tags=["trips", "toll"],
)
def get_trip_toll_reversal(trip_id: int):
    """按行程号查询等额冲销单。

    未知行程 404 TRIP_NOT_FOUND；未开单 404 BILL_NOT_FOUND；已开单但
    未作废（无冲销）404 REVERSAL_NOT_FOUND。冲销金额为非正整数分，
    与原账单绝对值相等；作废已开单行程时在原作废事务内生成，重复作废
    不重复冲销。
    """
    with get_conn() as conn:
        return get_reversal(conn, trip_id)


@app.get("/api/toll/bills", response_model=list[BillOut], tags=["toll"])
def list_toll_bills():
    """列出全部通行费账单（按行程号升序），各附冲销单（若已生成）。"""
    with get_conn() as conn:
        return list_bills(conn)


@app.post(
    "/api/trips/{trip_id}/review",
    response_model=TripReviewOut,
    tags=["trips"],
)
def review_trip(trip_id: int, body: Optional[ReviewRequest] = None):
    """潮位更正后复核已确认行程，并在不安全时求改期行程。

    只读操作：保持快照航段编号顺序、船舶参数、原起始分钟与规划窗口，仅允许
    在节点重新等待；改期择优为「变动航段数最少 → 抵达最早 → 出发分钟序列
    字典序最小」。窗口内无解时返回原行程风险与 no_solution，不修改快照。
    """
    target_revision = body.target_revision if body is not None else None
    with get_conn() as conn:
        result = review_and_reschedule(conn, trip_id, target_revision)
    return result


@app.post(
    "/api/trips/{trip_id}/reschedule",
    response_model=RescheduleConfirmOut,
    tags=["trips"],
)
def confirm_trip_reschedule(trip_id: int, body: RescheduleConfirmRequest):
    """凭已确认行程号、目标潮位修订号、复核建议中的逐段出发分钟与非空幂等键，
    执行**不安全航次的改期**（原行程作废 + 新行程确认，单事务幂等）。

    仅当原行程仍有效、目标修订为最新、原快照在该修订下不安全且窗口内有解时，
    按原航段顺序、船舶快照参数、起始分钟、窗口及泊位作业分钟，在同一事务内按
    **当前航时管制、狭窄水道组、泊位日历与其他有效占用（排除原行程）**重算最优
    建议；提交的逐段出发分钟须与复算最优建议逐段一致。通过后原行程作废
    （旧占用失效）、新快照确认（新占用生效）；原账单若存在在该事务内**等额
    冲销一次**，新行程待另行开单。

    - 未知行程 `404 TRIP_NOT_FOUND`；原行程已作废 `409 TRIP_ALREADY_VOIDED`；
    - 关联档案船已停用 `409 SHIP_DISABLED`、版本已变
      `409 SHIP_VERSION_STALE`、档案已删 `404 SHIP_NOT_FOUND`；
    - 目标修订未知 `404 TIDE_REVISION_NOT_FOUND`、已过期 `409 REVISION_STALE`；
    - 原快照仍安全 `409 RESCHEDULE_NOT_NEEDED`；窗口内无改期解
      `409 RESCHEDULE_NO_SOLUTION`；逐段出发分钟段数不符 `422 VALIDATION_ERROR`、
      与复算不一致 `409 PLAN_MISMATCH`（details 回传复算结果）；任一失败均
      **不改变状态**；
    - 同键同内容重试返回**首次结果**（200，replayed=true，即使此后修订又变化
      也先重放）；同键异内容 `409 RESCHEDULE_KEY_CONFLICT`。
    """
    with get_conn() as conn:
        result, replayed = confirm_reschedule(conn, trip_id, body)
    if replayed:
        return JSONResponse(status_code=200, content=result)
    return JSONResponse(status_code=201, content=result)


def _trip_out(d: dict) -> TripOut:
    return TripOut(
        trip_id=d["trip_id"],
        revision=d["revision"],
        origin=d["origin"],
        destination=d["destination"],
        start_minute=d["start_minute"],
        window_minutes=d["window_minutes"],
        draft=d["draft"],
        height=d["height"],
        margin=d["margin"],
        # 确认时固化的档案引用（无档案引用为 null）；后续档案变更不改写快照
        ship_no=d.get("ship_no"),
        ship_version=d.get("ship_version"),
        # 泊位作业快照（未指定泊位的旧确认为 null）；日历后续变更不改写作业区间
        berth_id=d.get("berth_id"),
        work_minutes=d.get("work_minutes"),
        work_start_minute=d.get("work_start_minute"),
        work_end_minute=d.get("work_end_minute"),
        arrival_minute=d["arrival_minute"],
        itinerary=json.loads(d["itinerary"]),
        confirmed_at=d["confirmed_at"],
        # 历史快照在作废后仍原样可查；voided_at 仅标识其占用已失效
        voided_at=d.get("voided_at"),
    )
