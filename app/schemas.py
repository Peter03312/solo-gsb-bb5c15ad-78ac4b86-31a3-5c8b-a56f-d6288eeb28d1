"""Pydantic 请求 / 响应模型。"""
from typing import Annotated, Optional

from pydantic import BaseModel, Field, model_validator

from . import config


# ---- 节点 / 航段 ----------------------------------------------------------------

class NodeIn(BaseModel):
    node_id: str = Field(min_length=1, description="节点编号，如 A")
    name: Optional[str] = None


class NodeOut(BaseModel):
    node_id: str
    name: Optional[str] = None


class SegmentIn(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    travel_time: int = Field(gt=0, description="航时（正整数分钟）")
    depth: float = Field(ge=0, description="静水深度（米）")
    clearance: Optional[float] = Field(
        default=None, ge=0, description="桥底净空（米），不填表示无桥"
    )


class SegmentOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    travel_time: int
    depth: float
    clearance: Optional[float] = None


# ---- 航段分时航时管制 -------------------------------------------------------------

class TravelControlIn(BaseModel):
    # 合法性（start>=0、end>start、travel_time>0）由服务层统一校验并返回
    # INVALID_CONTROL_INTERVAL，保证非法区间只有一个拒绝口径
    start_minute: int = Field(description="管制半开区间起点（含），绝对分钟，>=0")
    end_minute: int = Field(description="管制半开区间止点（不含），必须 > start_minute")
    travel_time: int = Field(description="该区间内进入航段的通行分钟（正整数）")


class TravelControlReplaceRequest(BaseModel):
    controls: list[TravelControlIn] = Field(
        description="整组替换的半开分钟区间清单（空组恢复航段基础航时）；"
                    "组内区间不得交叠"
    )


class TravelControlOut(TravelControlIn):
    pass


class TravelControlListOut(BaseModel):
    seg_id: int = Field(description="航段编号")
    base_travel_time: int = Field(description="航段基础航时（未命中管制区间时使用）")
    controls: list[TravelControlOut] = Field(
        description="当前生效的分时管制清单（按起点升序）；空列表表示全程基础航时"
    )


# ---- 狭窄水道组 -------------------------------------------------------------------

class ChannelGroupCreateRequest(BaseModel):
    segments: list[int] = Field(
        min_length=2,
        description="航段编号集合（至少 2 条）；组建成后不可修改，航段不得重复归组",
    )
    note: Optional[str] = None


class ChannelGroupMemberOut(BaseModel):
    position: int = Field(description="成员在提交集合中的次序（0 起）")
    seg_id: int = Field(description="成员航段编号")
    origin: str
    destination: str
    travel_time: int = Field(description="航段基础航时（正整数分钟）")
    depth: float
    clearance: Optional[float] = None


class ChannelGroupOut(BaseModel):
    group_id: int = Field(description="狭窄水道组号（自增、不可变）")
    note: Optional[str] = None
    created_at: str
    members: list[ChannelGroupMemberOut] = Field(
        description="组成员航段（按提交次序）；组建成后不可修改"
    )


# ---- 船舶档案 -----------------------------------------------------------------

class ShipCreateRequest(BaseModel):
    ship_no: str = Field(min_length=1, max_length=64, description="唯一船号")
    draft: float = Field(ge=0, description="非负吃水（米）")
    height: float = Field(ge=0, description="非负水面以上高度（米）")
    margin: float = Field(ge=0, description="非负安全余量（米）")
    enabled: bool = Field(default=True, description="启停状态：true 启用 / false 停用")
    note: Optional[str] = None


class ShipUpdateRequest(BaseModel):
    """档案变更：未给出的字段沿用当前值；任一字段实际变化才递增版本。"""
    draft: Optional[float] = Field(default=None, ge=0, description="非负吃水（米）")
    height: Optional[float] = Field(default=None, ge=0, description="非负水面以上高度（米）")
    margin: Optional[float] = Field(default=None, ge=0, description="非负安全余量（米）")
    enabled: Optional[bool] = Field(default=None, description="启停状态")
    note: Optional[str] = None


class ShipVersionOut(BaseModel):
    ship_no: str
    version: int = Field(description="档案版本号（自 1 起，有效变更递增）")
    draft: float
    height: float
    margin: float
    enabled: bool = Field(description="该版本的启停状态")
    note: Optional[str] = None
    created_at: str


class ShipOut(ShipVersionOut):
    """档案当前版本（最大版本号）。"""


class ShipUpdateResponse(ShipOut):
    changed: bool = Field(
        description="true=本次提交构成有效变更、版本已递增；false=与当前一致、版本未变"
    )


# ---- 潮位 ----------------------------------------------------------------------

class TideCorrection(BaseModel):
    corrections: dict[int, float] = Field(
        description="要覆盖的 {分钟: 名义潮位(米)}；未列出分钟继承基础修订名义潮位与误差"
    )
    base_revision: Optional[int] = Field(
        default=None, description="所基于的修订号；不填则取最新修订"
    )
    note: Optional[str] = None


class TideErrorRevision(BaseModel):
    errors: dict[int, float] = Field(
        description="要覆盖的 {分钟: 预测误差(米，非负)}；未列出分钟继承基础修订误差，"
                    "名义潮位不变"
    )
    base_revision: Optional[int] = Field(
        default=None, description="所基于的修订号；不填则取最新修订"
    )
    note: Optional[str] = None


class RevisionOut(BaseModel):
    revision: int
    note: Optional[str] = None
    created_at: str
    base_revision: Optional[int] = Field(
        default=None,
        description="本修订的基础修订号（允许从非最新修订分叉）；空表示基修订（全量）"
    )
    kind: Optional[str] = Field(
        default=None,
        description="修订类型：root=基修订（全量）/ correction=名义潮位更正 / error=预测误差修订"
    )


# ---- 规划 / 确认 ----------------------------------------------------------------

def _check_ship_xor_params(ship_no, draft, height, margin):
    """船号与三项参数（吃水/水面以上高度/安全余量）互斥且必居其一。

    混用两种输入（同时给出船号与任一参数）或两者皆缺 -> 422 VALIDATION_ERROR。
    """
    params = (draft, height, margin)
    if ship_no is not None:
        if any(p is not None for p in params):
            raise ValueError(
                "船号与 draft/height/margin 三项参数互斥："
                "混用两种输入被拒绝，请只保留船号或只提交三项参数"
            )
    elif any(p is None for p in params):
        raise ValueError(
            "未提供船号时，draft/height/margin 三项参数必须全部提供"
        )


def _check_ship_version_coupling(ship_no, ship_version):
    """确认时船号与档案版本必须成对出现（回填规划所得船号与版本）。"""
    if ship_no is None and ship_version is not None:
        raise ValueError("未提供船号时不得提供 ship_version")
    if ship_no is not None and ship_version is None:
        raise ValueError("按船号确认时必须回填规划所得档案版本 ship_version")


def _check_berth_coupling(berth_id, work_minutes):
    """终点泊位与作业分钟必须成对出现（二者同时给出或同时缺省）。"""
    if berth_id is None and work_minutes is not None:
        raise ValueError("未指定终点泊位 berth_id 时不得提供 work_minutes")
    if berth_id is not None and work_minutes is None:
        raise ValueError("指定终点泊位时必须同时提供正整数作业分钟 work_minutes")


class PlanRequest(BaseModel):
    origin: str
    destination: str
    start_minute: int = Field(ge=0, description="规划窗口起点（绝对分钟，>=0）")
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW, description="窗口长度，<=1440")
    draft: Optional[float] = Field(
        default=None, ge=0, description="船舶吃水（米）；与船号二选一"
    )
    height: Optional[float] = Field(
        default=None, ge=0, description="水面以上高度（米）；与船号二选一"
    )
    margin: Optional[float] = Field(
        default=None, ge=0, description="安全余量（米）；与船号二选一"
    )
    ship_no: Optional[str] = Field(
        default=None, min_length=1,
        description="船舶档案船号：代替三项参数，按当前启用版本规划",
    )
    revision: Optional[int] = Field(default=None, description="潮位修订号；不填用最新")
    berth_id: Optional[str] = Field(
        default=None, min_length=1,
        description="终点泊位编号：与 work_minutes 成对出现；船抵达终点即开工，"
                    "作业区间须完整落在该泊位同一开放区间且不与有效确认占用重叠",
    )
    work_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        description="正整数作业分钟：与 berth_id 成对出现；作业区间 [arrival, arrival+work)",
    )

    @model_validator(mode="after")
    def _ship_xor_params(self):
        _check_ship_xor_params(self.ship_no, self.draft, self.height, self.margin)
        return self

    @model_validator(mode="after")
    def _berth_coupled(self):
        _check_berth_coupling(self.berth_id, self.work_minutes)
        return self


class LegOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    start_minute: int
    end_minute: int
    travel_time: int


class PlanResponse(BaseModel):
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float = Field(description="本次规划实际所用吃水（直接提交或取自档案当前版本）")
    height: float = Field(description="本次规划实际所用水面以上高度")
    margin: float = Field(description="本次规划实际所用安全余量")
    ship_no: Optional[str] = Field(
        default=None, description="按档案规划时回传的船号；直接提交参数时为 null"
    )
    ship_version: Optional[int] = Field(
        default=None, description="按档案规划时回传的档案版本；直接提交参数时为 null"
    )
    berth_id: Optional[str] = Field(
        default=None,
        description="指定终点泊位预排时回传泊位编号；未指定泊位为 null（旧请求/响应不变）",
    )
    work_minutes: Optional[int] = Field(
        default=None, description="作业分钟（正整数）；未指定泊位为 null"
    )
    work_start_minute: Optional[int] = Field(
        default=None,
        description="作业区间起点（含）= 抵达分钟（抵达即开工）；未指定泊位为 null",
    )
    work_end_minute: Optional[int] = Field(
        default=None,
        description="作业区间止点（不含）= work_start_minute + work_minutes；未指定泊位为 null",
    )
    arrival_minute: int
    total_wait_minutes: int
    itinerary: list[LegOut]


class ConfirmRequest(PlanRequest):
    itinerary: list[LegOut] = Field(description="按规划结果原样回填的行程（含 seg_id/起止分钟）")
    ship_version: Optional[int] = Field(
        default=None, ge=1,
        description="按船号确认时必填：回填规划所得档案版本，事务内核对版本未变",
    )

    @model_validator(mode="after")
    def _ship_version_coupled(self):
        _check_ship_version_coupling(self.ship_no, self.ship_version)
        return self


# ---- 批次预排 / 确认（同一潮位修订下 2～5 艘船成批处理） ---------------------------

class BatchShipIn(BaseModel):
    """批次中的一艘船：字段语义与 PlanRequest 的单船字段一致。

    每艘船独立选择输入方式：船号（按当前启用版本）或三项参数，二者互斥。
    """
    origin: str
    destination: str
    start_minute: int = Field(ge=0, description="规划窗口起点（绝对分钟，>=0）")
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW, description="窗口长度，<=1440")
    draft: Optional[float] = Field(default=None, ge=0, description="船舶吃水（米）；与船号二选一")
    height: Optional[float] = Field(default=None, ge=0, description="水面以上高度（米）；与船号二选一")
    margin: Optional[float] = Field(default=None, ge=0, description="安全余量（米）；与船号二选一")
    ship_no: Optional[str] = Field(
        default=None, min_length=1,
        description="船舶档案船号：代替三项参数，按当前启用版本规划",
    )
    berth_id: Optional[str] = Field(
        default=None, min_length=1,
        description="终点泊位编号：与 work_minutes 成对出现；语义与单船预排一致",
    )
    work_minutes: Optional[int] = Field(
        default=None, ge=1,
        description="正整数作业分钟：与 berth_id 成对出现",
    )

    @model_validator(mode="after")
    def _ship_xor_params(self):
        _check_ship_xor_params(self.ship_no, self.draft, self.height, self.margin)
        return self

    @model_validator(mode="after")
    def _berth_coupled(self):
        _check_berth_coupling(self.berth_id, self.work_minutes)
        return self


class BatchPlanRequest(BaseModel):
    ships: list[BatchShipIn] = Field(
        min_length=2, max_length=5,
        description="按优先顺序排列的 2～5 艘船；排序靠后者须避开排序靠前者的拟占用",
    )
    revision: Optional[int] = Field(
        default=None, description="整批共用的潮位修订号；不填用最新修订"
    )


class BatchPlanShipOut(PlanResponse):
    index: int = Field(description="批次内序号（0 起，与请求中的优先顺序一致）")


class BatchPlanResponse(BaseModel):
    revision: int = Field(description="本次预排实际所据修订号（省略时解析为最新修订）")
    ships: list[BatchPlanShipOut]


class BatchShipConfirmIn(BatchShipIn):
    itinerary: list[LegOut] = Field(description="该船预排结果原样回填（含 seg_id/起止分钟）")
    ship_version: Optional[int] = Field(
        default=None, ge=1,
        description="按船号确认时必填：回填预排所得档案版本，事务内核对版本未变",
    )

    @model_validator(mode="after")
    def _ship_version_coupled(self):
        _check_ship_version_coupling(self.ship_no, self.ship_version)
        return self


class BatchConfirmRequest(BaseModel):
    ships: list[BatchShipConfirmIn] = Field(
        min_length=2, max_length=5,
        description="按预排时同一优先顺序排列的 2～5 艘船及其预排行程",
    )
    revision: int = Field(description="预排所据潮位修订号（批次确认必填）")
    idempotency_key: str = Field(
        min_length=1, max_length=128,
        description="批次幂等键：同键同内容重试返回原结果；同键异内容冲突",
    )


class BatchTripOut(BaseModel):
    index: int = Field(description="批次内序号（0 起，与请求顺序一致）")
    trip_id: int
    revision: int
    ship_no: Optional[str] = Field(
        default=None, description="按档案确认时固化的船号引用；直接提交参数时为 null"
    )
    ship_version: Optional[int] = Field(
        default=None, description="按档案确认时固化的档案版本；直接提交参数时为 null"
    )
    berth_id: Optional[str] = Field(
        default=None, description="确认时固化的终点泊位；未指定泊位为 null"
    )
    work_minutes: Optional[int] = Field(default=None, description="固化的作业分钟")
    work_start_minute: Optional[int] = Field(
        default=None, description="固化的作业区间起点（= 抵达分钟）"
    )
    work_end_minute: Optional[int] = Field(
        default=None, description="固化的作业区间止点（不含）"
    )
    arrival_minute: int
    itinerary: list[LegOut]


class BatchConfirmResponse(BaseModel):
    idempotency_key: str
    revision: int
    replayed: bool = Field(
        description="true=同键同内容重试，直接返回首次结果（未重新计算/保存）"
    )
    trips: list[BatchTripOut]


class TripOut(BaseModel):
    trip_id: int
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    ship_no: Optional[str] = Field(
        default=None,
        description="确认时固化的档案船号引用；直接提交参数确认的为 null。"
                    "后续档案变更不改写该快照",
    )
    ship_version: Optional[int] = Field(
        default=None, description="确认时固化的档案版本；无档案引用为 null"
    )
    berth_id: Optional[str] = Field(
        default=None,
        description="确认快照固化的终点泊位；未指定泊位的旧确认为 null，"
                    "日历后续变更不改写该作业区间",
    )
    work_minutes: Optional[int] = Field(default=None, description="快照固化的作业分钟")
    work_start_minute: Optional[int] = Field(
        default=None, description="快照固化的作业区间起点（= 抵达分钟，抵达即开工）"
    )
    work_end_minute: Optional[int] = Field(
        default=None, description="快照固化的作业区间止点（不含）"
    )
    arrival_minute: int
    itinerary: list[LegOut]
    confirmed_at: str
    voided_at: Optional[str] = Field(
        default=None,
        description="作废时间；未作废为 null。作废后确认快照仍作为历史快照原样可查，"
                    "仅其占用从后续调度失效",
    )


# ---- 已确认行程作废（误确认撤销） ---------------------------------------------------

class TripVoidRequest(BaseModel):
    reason: str = Field(
        min_length=1,
        description="非空作废原因（剥离首尾空白后至少 1 个字符）；重复作废不覆盖首次原因",
    )


class VoidRecordOut(BaseModel):
    trip_id: int = Field(description="被作废的已确认行程号")
    reason: str = Field(description="首次作废时提交的非空原因（重复作废原样返回）")
    voided_at: str = Field(description="首次作废时间")


class TripVoidResponse(VoidRecordOut):
    replayed: bool = Field(
        description="true=重复作废，返回的是首次记录（HTTP 200）；首次作废为 false（HTTP 201）",
    )


# ---- 潮位更正后的复核 / 改期 -------------------------------------------------------

class ReviewRequest(BaseModel):
    target_revision: Optional[int] = Field(
        default=None, description="目标潮位修订号；不填取最新修订"
    )


class ViolationOut(BaseModel):
    leg_index: int = Field(description="首次违规航段在快照行程中的下标（0 起）")
    seg_id: int
    origin: str
    destination: str
    minute: int = Field(description="首次违规的占用分钟（绝对分钟）")
    reasons: list[str] = Field(
        description="违规原因：depth=水深不足，clearance=桥高净空不足，missing_tide=缺潮位"
    )
    tide_level: Optional[float] = None
    tide_error: Optional[float] = Field(
        default=None, description="该分钟目标修订的预测误差（米）"
    )
    depth: float = Field(description="航段静水深度（米）")
    clearance: Optional[float] = Field(default=None, description="桥底净空（米），无桥为 null")
    required_depth: float = Field(description="要求的最小可用水深：吃水+余量（米）")
    required_clearance: float = Field(description="要求的最小净空：水面以上高度+余量（米）")
    available_depth: Optional[float] = Field(
        default=None, description="该分钟按误差带保守极值的实际可用水深：静水深度+名义潮位−误差（米）"
    )
    available_clearance: Optional[float] = Field(
        default=None,
        description="该分钟按误差带保守极值的实际桥高净空：桥底净空−名义潮位−误差（米），无桥为 null",
    )


class ChangedLegOut(BaseModel):
    leg_index: int
    seg_id: int
    original_departure_minute: int
    new_departure_minute: int


class RescheduleOut(BaseModel):
    revision: int = Field(description="改期所据目标修订号")
    arrival_minute: int
    total_wait_minutes: int
    changed_leg_count: int = Field(description="相对快照出发分钟发生变化的航段个数")
    changed_legs: list[ChangedLegOut]
    itinerary: list[LegOut]
    berth_id: Optional[str] = Field(
        default=None,
        description="原快照指定泊位时，改期建议同时找到的原泊位可用作业区间所属泊位"
                    "（只建议不预留）；未指定泊位为 null",
    )
    work_minutes: Optional[int] = Field(default=None, description="作业分钟（沿用快照）")
    work_start_minute: Optional[int] = Field(
        default=None, description="建议作业区间起点（= 建议抵达分钟，抵达即开工）"
    )
    work_end_minute: Optional[int] = Field(
        default=None, description="建议作业区间止点（不含）"
    )


class TripReviewOut(BaseModel):
    trip_id: int
    snapshot_revision: int = Field(description="确认快照所据修订号")
    target_revision: int = Field(description="本次复核所据目标修订号")
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    original_arrival_minute: int
    berth_id: Optional[str] = Field(
        default=None,
        description="确认快照固化的终点泊位（潮位复核仍只按原快照判断航路安全，"
                    "与泊位日历/占用无关）；未指定泊位为 null",
    )
    work_minutes: Optional[int] = Field(default=None, description="快照固化的作业分钟")
    work_start_minute: Optional[int] = Field(
        default=None, description="快照固化的作业区间起点"
    )
    work_end_minute: Optional[int] = Field(
        default=None, description="快照固化的作业区间止点（不含）"
    )
    safe: bool = Field(description="原行程在目标修订下按原占用分钟是否安全")
    first_violation: Optional[ViolationOut] = Field(
        default=None, description="不安全时的首次违规航段/分钟/原因"
    )
    reschedule: Optional[RescheduleOut] = Field(
        default=None, description="不安全且窗口内有改期解时的改期行程"
    )
    no_solution: bool = Field(
        default=False, description="不安全且窗口内无改期解时为 true（快照不被修改）"
    )
    original_itinerary: list[LegOut]


# ---- 通行费费率版本 ----------------------------------------------------------------

class TollRateIn(BaseModel):
    # 合法性（start>=0、end>start、费率为非负整数）由服务层统一校验并返回
    # INVALID_TOLL_RATE_INTERVAL，保证非法区间只有一个拒绝口径
    start_minute: int = Field(description="费率半开区间起点（含），绝对分钟，>=0")
    end_minute: int = Field(description="费率半开区间止点（不含），必须 > start_minute")
    rate_cents_per_minute: int = Field(
        description="该区间内进入航段每分钟的通行费（非负整数分/分钟）"
    )


class TollRateReplaceRequest(BaseModel):
    rates: list[TollRateIn] = Field(
        description="整组替换的半开分钟费率区间清单（空组表示该航段未覆盖分钟费率为 0）；"
                    "组内区间不得交叠"
    )
    note: Optional[str] = Field(
        default=None, description="新版本备注；不填自动生成（注明航段与所继承版本）"
    )


class TollRateOut(TollRateIn):
    seg_id: int = Field(description="航段编号")


class TollRateVersionOut(BaseModel):
    version: int = Field(description="费率版本号（自增、不可变）")
    changed_seg_id: int = Field(description="本版本被整组替换费率的航段")
    note: Optional[str] = None
    created_at: str
    rates: list[TollRateOut] = Field(
        description="该版本下全部航段的费率区间（按航段编号、起点升序）；"
                    "未列出的航段/分钟费率均为 0"
    )


# ---- 通行费账单 / 冲销单 -------------------------------------------------------------

class BillSegmentOut(BaseModel):
    leg_index: int = Field(description="该计费航段在确认快照行程中的下标（0 起）")
    seg_id: int
    origin: str
    destination: str
    entry_minute: int = Field(description="进入航段的分钟（取自确认快照的 start_minute）")
    travel_time: int = Field(description="固化航时（分钟），取自确认快照，不随后续变化")
    rate_cents_per_minute: int = Field(
        description="按进入分钟命中费率版本区间所选费率（分/分钟）；未覆盖为 0"
    )
    amount_cents: int = Field(
        description="该段金额（整数分）= travel_time × rate_cents_per_minute"
    )


class ReversalOut(BaseModel):
    trip_id: int = Field(description="被冲销账单的行程号")
    version: int = Field(description="冲销沿用的费率版本（即原账单固化版本，不可变）")
    total_cents: int = Field(
        description="冲销总额（非正整数分）：与原账单等额相反（账单为正、冲销为负）"
    )
    segments: list[BillSegmentOut] = Field(
        description="逐段冲销结果：金额为原账单各段的相反数"
    )
    reversed_at: str = Field(description="冲销生成时间（即首次作废时间）")


class BillOut(BaseModel):
    trip_id: int
    version: int = Field(description="开单时固化的费率版本号（旧账单不随新版本改变）")
    total_cents: int = Field(description="账单总额（非负整数分）= 各段金额之和")
    segments: list[BillSegmentOut] = Field(description="逐段计费结果（按快照航段顺序）")
    billed_at: str
    reversal: Optional[ReversalOut] = Field(
        default=None,
        description="该行程作废后生成的等额冲销单；未作废或作废时未开单时为 null",
    )


class BillResponse(BillOut):
    replayed: bool = Field(
        description="true=重复开单重试，返回的是首次账单（HTTP 200）；首次开单为 false（HTTP 201）",
    )


# ---- 不安全航次改期确认（原行程作废 + 新行程确认，幂等） ----------------------------

class RescheduleConfirmRequest(BaseModel):
    target_revision: int = Field(
        description="改期所据目标潮位修订号（必填；事务内核验其仍为最新）"
    )
    departure_minutes: list[Annotated[int, Field(ge=0)]] = Field(
        description="复核建议 reschedule.itinerary 中逐段出发分钟（绝对分钟，"
                    "按原航段顺序，均为非负整数）；个数必须与原快照航段数一致，"
                    "事务内与复算最优建议逐段比对"
    )
    idempotency_key: str = Field(
        min_length=1, max_length=128,
        description="非空改期幂等键：同键同内容重试返回首次结果；同键异内容冲突",
    )


class RescheduleVoidOut(BaseModel):
    trip_id: int = Field(description="被作废的原行程号")
    reason: str = Field(description="本次改期自动作废原行程时的不可变系统原因")
    voided_at: str = Field(description="原行程作废时间（即改期确认时间）")


class RescheduleConfirmOut(BaseModel):
    idempotency_key: str
    old_trip_id: int = Field(description="被作废的原确认行程号（历史快照仍可查）")
    new_trip_id: int = Field(description="改期后新确认的行程号（待另行开单）")
    snapshot_revision: int = Field(description="原确认快照所据修订号")
    target_revision: int = Field(description="本次改期所据（新快照固化的）修订号")
    replayed: bool = Field(
        description="true=同键同内容重试，返回首次结果（HTTP 200）；首次执行为 false（HTTP 201）"
    )
    reschedule: RescheduleOut = Field(
        description="事务内按当前航时管制、狭窄水道组、泊位日历与其他有效占用"
                    "（排除原行程）复算并与提交逐段比对一致的改期行程（即新快照内容）"
    )
    old_void: RescheduleVoidOut = Field(description="原行程作废记录")
    reversal_created: bool = Field(
        description="true=原账单存在，已在同一事务内等额冲销一次；false=原行程未开单、无冲销"
    )
    reversal: Optional[ReversalOut] = Field(
        default=None,
        description="原账单的等额冲销单（reversal_created=true 时给出）；"
                    "未开单时为 null，新行程待另行开单",
    )


# ---- 终点泊位与开放日历 -------------------------------------------------------------

class BerthCreateRequest(BaseModel):
    berth_id: str = Field(min_length=1, max_length=64, description="全局唯一泊位编号")
    note: Optional[str] = None


class BerthIntervalIn(BaseModel):
    # 合法性（start>=0、end>start）由服务层统一校验并返回
    # INVALID_BERTH_INTERVAL，保证非法区间只有一个拒绝口径
    start_minute: int = Field(description="开放半开区间起点（含），绝对分钟，>=0")
    end_minute: int = Field(description="开放半开区间止点（不含），必须 > start_minute")


class BerthIntervalOut(BerthIntervalIn):
    pass


class BerthCalendarReplaceRequest(BaseModel):
    intervals: list[BerthIntervalIn] = Field(
        description="整组替换的半开分钟开放区间清单（空组表示该泊位全程不开放）；"
                    "组内区间不得交叠（首尾相接合法）"
    )


class BerthOut(BaseModel):
    berth_id: str = Field(description="泊位编号（全局唯一）")
    node_id: str = Field(description="所属节点编号（建成即固定）")
    note: Optional[str] = None
    created_at: str
    calendar: list[BerthIntervalOut] = Field(
        description="当前生效的开放日历（按起点升序）；空列表表示全程不开放"
    )
