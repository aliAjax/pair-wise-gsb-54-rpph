"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, NotFound, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'register_batch': {'noc_operator', 'repair_manager'}, 'confirm_batch': {'noc_operator', 'repair_manager'}, 'revoke_batch': {'noc_operator', 'repair_manager'}, 'complete_restoration': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'register_batch': {'tested': 'restoring', 'restoring': 'restoring'}, 'confirm_batch': {'restoring': 'restoring'}, 'revoke_batch': {'restoring': 'restoring'}, 'complete_restoration': {'restoring': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}
RESTORATION_ACTIONS = {'register_batch', 'confirm_batch', 'revoke_batch', 'complete_restoration'}


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

    def plan_restoration(self, record: Dict[str, Any], action: str, data: Dict[str, Any], batches: List[Dict[str, Any]]) -> Dict[str, Any]:
        """校验分批复通动作并返回台账变更计划（纯规则，不落库）。"""
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        capacity_total = int(record["payload"]["capacity_gbps"])
        active = [item for item in batches if item["status"] in ("pending", "confirmed")]
        if action == "register_batch":
            route = text(data, "route")
            capacity = integer(data, "capacity_gbps", 1)
            groups = text_list(data, "customer_groups", 1)
            if any(item["route"] == route for item in active):
                raise Conflict("该路由已在其他复通批次中占用")
            existing_groups = {group for item in active for group in item["customer_groups"]}
            duplicated = sorted(set(groups) & existing_groups)
            if duplicated:
                raise Conflict("客户组%s已在其他复通批次中占用" % "、".join(duplicated))
            reserved = sum(int(item["capacity_gbps"]) for item in active)
            if reserved + capacity > capacity_total:
                raise ValidationError("累计容量%s超过故障前容量%s" % (reserved + capacity, capacity_total))
            batch_no = len(batches) + 1
            return {"action": action, "state": new_state, "batch_no": batch_no, "batch": {"batch_no": batch_no, "route": route, "capacity_gbps": capacity, "customer_groups": groups}, "summary": "第%s批复通已登记，待确认" % batch_no}
        if action == "complete_restoration":
            pending = [item for item in active if item["status"] == "pending"]
            confirmed_capacity = sum(int(item["capacity_gbps"]) for item in active if item["status"] == "confirmed")
            if pending:
                raise Conflict("仍有%s个待确认批次，不能结束工单" % len(pending))
            if confirmed_capacity < capacity_total:
                raise ValidationError("已确认容量%s未补齐故障前容量%s" % (confirmed_capacity, capacity_total))
            return {"action": action, "state": new_state, "summary": "容量已补齐，分批复通完成"}
        batch_no = integer(data, "batch_no", 1)
        target = next((item for item in batches if item["batch_no"] == batch_no), None)
        if target is None:
            raise NotFound("复通批次不存在")
        if action == "confirm_batch":
            if target["status"] != "pending":
                raise Conflict("第%s批不是待确认状态" % batch_no)
            return {"action": action, "state": new_state, "batch_no": batch_no, "summary": "第%s批复通已确认，容量计入可用" % batch_no}
        if target["status"] not in ("pending", "confirmed"):
            raise Conflict("第%s批已撤销，不能重复撤销" % batch_no)
        return {"action": action, "state": new_state, "batch_no": batch_no, "summary": "第%s批复通已撤销，仅回退该批容量与占用" % batch_no}
