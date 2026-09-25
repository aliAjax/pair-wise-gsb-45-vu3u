"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text


INITIAL_STATE = "draft"
RESCHEDULE_STATE = "reschedule_required"

# 潮汐表采用0-24时的整数小时模型，元组为涨潮时段[start, end)，每日两次涨潮
DEFAULT_FLOOD_WINDOWS: Tuple[Tuple[int, int], ...] = ((2, 8), (14, 20))
# 落潮/平潮期航道可保证的安全吃水；超过该值的深吃水船只能排涨潮时段
DEFAULT_TIDE_SAFE_DRAFT_M = 9.0
# 达到该长度即视为超长船，须与危险品船一样独占通航时段
DEFAULT_EXCLUSIVE_LENGTH_M = 250.0
# 处于这些状态的计划不再占用航道通航时段
CHANNEL_RELEASED_STATES = {"cancelled", "departed", RESCHEDULE_STATE}

CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'reschedule': {'port_controller'}, 'cancel': {'port_controller'}}
TRANSITIONS = {
    'confirm': {'draft': ('confirmed', RESCHEDULE_STATE)},
    'berth': {'confirmed': ('berthed',)},
    'depart': {'berthed': ('departed',)},
    'reschedule': {RESCHEDULE_STATE: ('confirmed',)},
    'cancel': {'draft': ('cancelled',), 'confirmed': ('cancelled',), RESCHEDULE_STATE: ('cancelled',)},
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    RESCHEDULE_STATE = RESCHEDULE_STATE

    def __init__(
        self,
        tide_safe_draft_m: float = DEFAULT_TIDE_SAFE_DRAFT_M,
        exclusive_length_m: float = DEFAULT_EXCLUSIVE_LENGTH_M,
        flood_tide_windows: Tuple[Tuple[int, int], ...] = DEFAULT_FLOOD_WINDOWS,
    ) -> None:
        self.tide_safe_draft_m = float(tide_safe_draft_m)
        self.exclusive_length_m = float(exclusive_length_m)
        self.flood_tide_windows = tuple(flood_tide_windows)

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or ACTION_ROLES.get(action, set())

    def _in_flood_window(self, start_hour: int, end_hour: int) -> bool:
        return any(start_hour >= left and end_hour <= right for left, right in self.flood_tide_windows)

    def _channel_exclusive(self, payload: Dict[str, Any]) -> bool:
        return bool(payload.get("dangerous_goods")) or float(payload.get("vessel_length_m", 0)) >= self.exclusive_length_m

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
        transit_start = integer(p, "transit_start_hour", 0, 23)
        transit_end = integer(p, "transit_end_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if transit_end <= transit_start:
            raise ValidationError("通航时段结束必须晚于开始")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        transit_start = int(p["transit_start_hour"])
        transit_end = int(p["transit_end_hour"])
        deep_draft = float(p["draft_m"]) > self.tide_safe_draft_m
        if deep_draft and not self._in_flood_window(transit_start, transit_end):
            raise ValidationError("吃水超过潮汐安全值，只能安排涨潮时段通航")
        p["deep_draft"] = deep_draft
        p["flood_tide_required"] = deep_draft
        p["channel_exclusive"] = self._channel_exclusive(p)
        # 通航时段必须赶在靠泊开始前；登记时先标记，确认时晚于ETA则整体待重排
        p["nav_on_time"] = transit_end <= int(p["eta_hour"])
        p["nav_status"] = "registered"
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    def check_channel_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]], self_id: Optional[int] = None) -> None:
        start = int(payload["transit_start_hour"])
        end = int(payload["transit_end_hour"])
        incoming_exclusive = self._channel_exclusive(payload)
        for item in existing:
            if self_id is not None and int(item.get("id", -1)) == int(self_id):
                continue
            if item["state"] in CHANNEL_RELEASED_STATES:
                continue
            other = item["payload"]
            if "transit_start_hour" not in other or "transit_end_hour" not in other:
                continue
            other_start = int(other["transit_start_hour"])
            other_end = int(other["transit_end_hour"])
            if start < other_end and end > other_start:
                vessel = other.get("vessel", "其他船舶")
                if incoming_exclusive:
                    raise Conflict("危险品或超长船舶须独占通航时段，与%s的航道时段重叠" % vessel)
                if self._channel_exclusive(other):
                    raise Conflict("通航时段与独占船舶%s重叠，其他船舶不能并排通过" % vessel)

    def require_transition(self, record: Dict[str, Any], action: str) -> Tuple[str, ...]:
        targets = TRANSITIONS.get(action, {}).get(record["state"])
        if not targets:
            raise Conflict("当前状态不允许执行%s" % action)
        return tuple(targets)

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], existing: Iterable[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any], str]:
        targets = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        new_state = targets[0]
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            if int(p["transit_end_hour"]) > int(p["eta_hour"]):
                new_state = RESCHEDULE_STATE
                reason = "通航时段晚于靠泊开始，通航与靠泊计划待重排"
                changes["nav_on_time"] = False
                changes["nav_status"] = RESCHEDULE_STATE
                changes["reschedule_reason"] = reason
                summary = reason
            else:
                self.check_channel_conflicts(p, existing or [], self_id=record.get("id"))
                changes["nav_status"] = "confirmed"
                summary = "已确认引航员与航道通航时段"
        elif action == "reschedule":
            transit_start = integer(data, "transit_start_hour", 0, 23)
            transit_end = integer(data, "transit_end_hour", 1, 24)
            if transit_end <= transit_start:
                raise ValidationError("通航时段结束必须晚于开始")
            if transit_end > int(p["eta_hour"]):
                raise ValidationError("重排后的通航时段仍晚于靠泊开始时间")
            if float(p["draft_m"]) > self.tide_safe_draft_m and not self._in_flood_window(transit_start, transit_end):
                raise ValidationError("吃水超过潮汐安全值，只能安排涨潮时段通航")
            tentative = dict(p)
            tentative["transit_start_hour"] = transit_start
            tentative["transit_end_hour"] = transit_end
            self.check_channel_conflicts(tentative, existing or [], self_id=record.get("id"))
            changes["transit_start_hour"] = transit_start
            changes["transit_end_hour"] = transit_end
            changes["nav_on_time"] = True
            changes["nav_status"] = "confirmed"
            summary = "通航时段已重排，靠泊计划恢复确认"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            # 离泊出港后航道时段自动让出，冲突检查不再计入本记录
            changes["nav_status"] = "released"
            summary = "船舶已离泊出港，航道通航时段自动让出"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        if new_state not in targets:
            raise Conflict("当前状态不允许执行%s" % action)
        p.update(changes)
        if action == "reschedule":
            p.pop("reschedule_reason", None)
        return new_state, p, summary or ("已执行%s" % action)
