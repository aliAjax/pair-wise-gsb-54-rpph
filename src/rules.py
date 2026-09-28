"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "detected"
RESTORING_STATE = "restoring"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'register_batch': {'noc_operator', 'repair_manager'}, 'confirm_batch': {'noc_operator', 'repair_manager'}, 'revert_batch': {'noc_operator', 'repair_manager'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'register_batch': {'tested': 'restoring', 'restoring': 'restoring'}, 'confirm_batch': {'restoring': 'restoring'}, 'revert_batch': {'restoring': 'restoring'}, 'restore': {'restoring': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}

BATCH_PENDING = "pending"
BATCH_CONFIRMED = "confirmed"
BATCH_REVERTED = "reverted"
LEDGER_ACTIONS = {'register_batch', 'confirm_batch', 'revert_batch'}


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

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

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
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            if float(data.get("spare_used_km", 0)) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = float(data["spare_used_km"])
            summary = "光缆接续完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    @staticmethod
    def _active_batches(batches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [batch for batch in batches if batch["status"] != BATCH_REVERTED]

    @staticmethod
    def _find_batch(batches: List[Dict[str, Any]], seq: int) -> Dict[str, Any]:
        for batch in batches:
            if int(batch["seq"]) == int(seq):
                return batch
        raise Conflict("第%s批复通批次不存在" % seq)

    def _pre_failure_capacity(self, record: Dict[str, Any]) -> int:
        return int(record["payload"]["capacity_gbps"])

    def plan_register_batch(self, record: Dict[str, Any], batches: List[Dict[str, Any]], data: Dict[str, Any]) -> Dict[str, Any]:
        data = data or {}
        route = text(data, "route")
        capacity = integer(data, "capacity_gbps", 1)
        customer_groups = text_list(data, "customer_groups", 1)
        if len(set(customer_groups)) != len(customer_groups):
            raise ValidationError("同一批次内客户组不能重复")
        total = self._pre_failure_capacity(record)
        active = self._active_batches(batches)
        registered = sum(int(batch["capacity_gbps"]) for batch in active)
        if registered + capacity > total:
            raise Conflict("累计容量不得超过故障前容量，剩余可登记容量%sGbps" % (total - registered))
        if any(batch["route"] == route for batch in active):
            raise Conflict("路由%s已被其他批次占用" % route)
        occupied_groups = set()
        for batch in active:
            occupied_groups.update(batch["customer_groups"])
        duplicated = sorted({group for group in customer_groups if group in occupied_groups})
        if duplicated:
            raise Conflict("客户组%s已被其他批次占用" % "、".join(duplicated))
        seq = max([int(batch["seq"]) for batch in batches] + [0]) + 1
        return {"seq": seq, "route": route, "capacity_gbps": capacity, "customer_groups": customer_groups, "status": BATCH_PENDING}

    def plan_confirm_batch(self, batches: List[Dict[str, Any]], data: Dict[str, Any]) -> Dict[str, Any]:
        seq = integer(data or {}, "seq", 1)
        batch = self._find_batch(batches, seq)
        if batch["status"] != BATCH_PENDING:
            raise Conflict("第%s批当前为%s状态，不能确认" % (seq, batch["status"]))
        return {"seq": seq}

    def plan_revert_batch(self, batches: List[Dict[str, Any]], data: Dict[str, Any]) -> Dict[str, Any]:
        seq = integer(data or {}, "seq", 1)
        batch = self._find_batch(batches, seq)
        if batch["status"] == BATCH_REVERTED:
            raise Conflict("第%s批已经撤销" % seq)
        return {"seq": seq, "previous_status": batch["status"]}

    def check_restore_finish(self, record: Dict[str, Any], batches: List[Dict[str, Any]]) -> Dict[str, int]:
        total = self._pre_failure_capacity(record)
        active = self._active_batches(batches)
        confirmed = sum(int(batch["capacity_gbps"]) for batch in active if batch["status"] == BATCH_CONFIRMED)
        pending = [batch for batch in active if batch["status"] == BATCH_PENDING]
        if pending:
            raise Conflict("仍有%s批待确认，不能结束工单" % len(pending))
        if confirmed < total:
            raise Conflict("容量尚未补齐，剩余缺口%sGbps" % (total - confirmed))
        return {"pre_failure_capacity_gbps": total, "confirmed_capacity_gbps": confirmed}

    def build_restore_ledger(self, record: Dict[str, Any], batches: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = self._pre_failure_capacity(record)
        active = self._active_batches(batches)
        registered = sum(int(batch["capacity_gbps"]) for batch in active)
        confirmed = sum(int(batch["capacity_gbps"]) for batch in active if batch["status"] == BATCH_CONFIRMED)
        pending_count = sum(1 for batch in active if batch["status"] == BATCH_PENDING)
        return {"pre_failure_capacity_gbps": total, "registered_capacity_gbps": registered, "confirmed_capacity_gbps": confirmed, "remaining_gap_gbps": max(0, total - confirmed), "pending_count": pending_count, "batches": batches}
