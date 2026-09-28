"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import LEDGER_ACTIONS, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def _with_ledger(self, record: Dict[str, Any]) -> Dict[str, Any]:
        if record["state"] in {"tested", "restoring", "restored"}:
            batches = self.repository.list_restore_batches(record["id"])
            record["restore_ledger"] = self.rules.build_restore_ledger(record, batches)
        return record

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_ledger(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        if action == "restore":
            return self._finish_restore(actor, record, int(expected_version), data or {})
        if action in LEDGER_ACTIONS:
            return self._ledger_act(actor, record, int(expected_version), action, data or {})
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        return self._with_ledger(result)

    def _finish_restore(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state = self.rules.require_transition(record, "restore")
        batches = self.repository.list_restore_batches(record["id"])
        capacity = self.rules.check_restore_finish(record, batches)
        new_payload = dict(record["payload"])
        new_payload["traffic_restored"] = True
        new_payload["restore_capacity_gbps"] = capacity["confirmed_capacity_gbps"]
        summary = "通信已全部恢复（%sGbps）" % capacity["confirmed_capacity_gbps"]
        result = self.repository.mutate(
            record_id=record["id"],
            expected_version=expected_version,
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action="restore",
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state, "capacity": capacity},
        )
        return self._with_ledger(result)

    def _ledger_act(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state = self.rules.require_transition(record, action)
        batches = self.repository.list_restore_batches(record["id"])
        ledger_op: Dict[str, Any]
        if action == "register_batch":
            planned = self.rules.plan_register_batch(record, batches, data)
            ledger_op = {"op": "register", "batch": planned}
            summary = "第%s批复通已登记：路由%s，容量%sGbps，客户组%s" % (planned["seq"], planned["route"], planned["capacity_gbps"], "、".join(planned["customer_groups"]))
        elif action == "confirm_batch":
            planned = self.rules.plan_confirm_batch(batches, data)
            ledger_op = {"op": "confirm", "seq": planned["seq"]}
            summary = "第%s批复通已确认，容量计入可用容量" % planned["seq"]
        else:
            planned = self.rules.plan_revert_batch(batches, data)
            ledger_op = {"op": "revert", "seq": planned["seq"]}
            summary = "第%s批复通已撤销，仅回退该批容量与占用" % planned["seq"]
        result = self.repository.mutate_ledger(
            record_id=record["id"],
            expected_version=expected_version,
            state=new_state,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state, "batch": planned},
            ledger_op=ledger_op,
            actor_id=actor.user_id,
        )
        return self._with_ledger(result)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
