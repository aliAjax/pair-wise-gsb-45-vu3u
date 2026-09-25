"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"

# 航道通航调度参数
TIDE_SAFE_DRAFT_M = 10.0  # 潮汐安全吃水（米）：超过该值只能在涨潮时段通航
OVERLENGTH_M = 200.0  # 超长船阈值（米）：达到即独占通航时段
# 涨潮时段（小时，半开区间[h, h+1)）；本原型按半日潮给出两个涨潮窗口
FLOOD_TIDE_HOURS = frozenset({2, 3, 4, 5, 14, 15, 16, 17})

TRANSIT_REGISTERED = "registered"
TRANSIT_PENDING = "pending_reschedule"
TRANSIT_RELEASED = "released"
BERTH_SCHEDULED = "scheduled"
BERTH_PENDING = "pending_reschedule"

CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}, 'reschedule': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}, 'reschedule': {'draft': 'draft'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def _transit_window(self, data: Dict[str, Any]) -> Tuple[int, int]:
        start = integer(data, "transit_start_hour", 0, 23)
        end = integer(data, "transit_end_hour", 1, 24)
        if end <= start:
            raise ValidationError("通航结束时段必须晚于开始时段")
        return start, end

    def _ensure_flood_tide(self, draft: float, start: int, end: int) -> None:
        if draft > TIDE_SAFE_DRAFT_M and not set(range(start, end)) <= FLOOD_TIDE_HOURS:
            raise ValidationError("吃水超过潮汐安全值%s米，只能安排涨潮时段通航" % TIDE_SAFE_DRAFT_M)

    @staticmethod
    def _transit_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
        """按时序计算通航/靠泊的编排状态：通航晚于靠泊开始则一并待重排。"""
        late = int(payload["transit_end_hour"]) > int(payload["eta_hour"])
        return {
            "reschedule_required": late,
            "reschedule_reason": "通航时段晚于靠泊开始时间，通航与靠泊待重排" if late else "",
            "transit_status": TRANSIT_PENDING if late else TRANSIT_REGISTERED,
            "berth_status": BERTH_PENDING if late else BERTH_SCHEDULED,
        }

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        transit_start, transit_end = self._transit_window(p)
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        # 深吃水船只能排涨潮时段
        self._ensure_flood_tide(draft, transit_start, transit_end)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        # 航道通航登记信息
        p["flood_tide_required"] = float(p["draft_m"]) > TIDE_SAFE_DRAFT_M
        p["transit_exclusive"] = bool(p["dangerous_goods"]) or float(p["vessel_length_m"]) > OVERLENGTH_M
        p.update(self._transit_plan(p))
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    def check_channel_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]], exclude_id: Optional[int] = None) -> None:
        """航道独占校验：危险品/超长船的通航时段不允许与任何船舶重叠。"""
        if payload.get("transit_status") == TRANSIT_PENDING:
            return  # 待重排计划尚未取得有效通航时段，不占用航道
        start = int(payload["transit_start_hour"])
        end = int(payload["transit_end_hour"])
        for item in existing:
            if exclude_id is not None and int(item.get("id", -1)) == int(exclude_id):
                continue
            if item["state"] in {"cancelled", "departed"}:
                continue  # 离泊出港后通航时段自动让出
            other = item["payload"]
            if other.get("transit_status") in {TRANSIT_PENDING, TRANSIT_RELEASED}:
                continue
            if start < int(other["transit_end_hour"]) and end > int(other["transit_start_hour"]):
                if payload.get("transit_exclusive") or other.get("transit_exclusive"):
                    raise Conflict("通航时段冲突：危险品或超长船独占时段，禁止与其他船舶并排通航")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action in {"confirm", "berth"} and p.get("berth_status") == BERTH_PENDING:
            raise Conflict("通航与靠泊处于待重排状态，请先执行reschedule")
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            # 离泊出港，占用的通航时段自动让出
            changes["transit_status"] = TRANSIT_RELEASED
            summary = "船舶已离泊出港，占用通航时段已自动让出"
        elif action == "reschedule":
            start, end = self._transit_window(data)
            self._ensure_flood_tide(float(p["draft_m"]), start, end)
            changes["transit_start_hour"] = start
            changes["transit_end_hour"] = end
            merged = dict(p, **changes)
            changes.update(self._transit_plan(merged))
            summary = "通航时段已重新编排" if not changes["reschedule_required"] else "通航仍晚于靠泊开始，继续待重排"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
