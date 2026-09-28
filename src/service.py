"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import RESTORATION_ACTIONS, DomainRules


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

    @staticmethod
    def _restoration_view(record: Dict[str, Any], batches: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = int(record["payload"]["capacity_gbps"])
        pending = [item for item in batches if item["status"] == "pending"]
        confirmed = [item for item in batches if item["status"] == "confirmed"]
        revoked = [item for item in batches if item["status"] == "revoked"]
        pending_capacity = sum(int(item["capacity_gbps"]) for item in pending)
        confirmed_capacity = sum(int(item["capacity_gbps"]) for item in confirmed)
        return {
            "pre_failure_capacity_gbps": total,
            "confirmed_capacity_gbps": confirmed_capacity,
            "pending_capacity_gbps": pending_capacity,
            "remaining_gap_gbps": max(0, total - confirmed_capacity),
            "capacity_complete": confirmed_capacity >= total,
            "pending_batches": len(pending),
            "can_complete": not pending and confirmed_capacity >= total,
            "batches": batches,
            "counts": {"pending": len(pending), "confirmed": len(confirmed), "revoked": len(revoked)},
        }

    def _with_restoration(self, record: Dict[str, Any]) -> Dict[str, Any]:
        batches = self.repository.list_batches(int(record["id"]))
        if batches:
            record["restoration"] = self._restoration_view(record, batches)
        return record

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_restoration(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        if action in RESTORATION_ACTIONS:
            batches = self.repository.list_batches(record_id)
            plan = self.rules.plan_restoration(record, action, data or {}, batches)
            updated, updated_batches = self.repository.apply_restoration(
                record_id=record_id,
                expected_version=int(expected_version),
                plan=plan,
                payload=record["payload"],
                actor_id=actor.user_id,
                input_data=data or {},
            )
            return self._with_restoration(updated)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
