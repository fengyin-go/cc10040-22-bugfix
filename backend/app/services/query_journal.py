"""查询日志领域服务：记录和审计所有分析查询的调用链路和性能。

跨模块数据流: query_builder/data_service -> query_journal（记录查询）
              cache_layer -> query_journal（区分缓存与实时查询）

账务口径：
  * 一笔执行只有一个 execution_id，跨“缓存命中 / 冷执行 / 旧日志”都在同一本账里；
  * 只有带可核实指标身份的执行才进入该指标的次数与耗时；
  * 证明不了归属的执行挂在待认领，不混进任何指标，补齐依据后仍可并回。
"""
from __future__ import annotations

from typing import Any

from app.store import UNCLAIMED, store
from app.services.base import BaseModuleService


class QueryJournalService(BaseModuleService):
    MODULE = "query_journal"
    ENTITY = "查询日志"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["查询时间", "操作人", "涉及指标", "查询语句摘要", "返回行数"]
    REQUIRED_FIELDS = ["查询时间", "操作人", "涉及指标"]
    STATUS_ORDER = ["正常", "慢查询", "异常终止"]
    ACTION_RULES = {"标记慢查询": "慢查询", "忽略告警": "正常"}
    NEGATIVE_ACTIONS = ["忽略告警"]

    def list_entries(self, *, filters: dict[str, str] | None = None,
                     keyword: str | None = None, status: str | None = None,
                     page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        """列表与统计同源：都读执行账本，旧执行和新执行在同一份明细里。"""
        rows = store.execution_snapshot()
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [row for row in rows
                        if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows
                    if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find_execution(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """手工登记一条查询日志；没有可核实身份时进入待认领，而不是凭空归属。"""
        missing = [field for field in self.REQUIRED_FIELDS
                   if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        metric_name = str(values.get("涉及指标") or "").strip()
        metric = next((row for row in store.rows("metric_definition")
                       if row.get("指标名称") == metric_name), None)
        entry = store.record_execution(
            operator=str(values.get("操作人", "")),
            sql_summary=str(values.get("查询语句摘要", "")),
            row_count=int(str(values.get("返回行数") or 0).strip() or 0)
            if str(values.get("返回行数") or "").strip().isdigit() else 0,
            duration_ms=0.0,
            status=str(values.get("status") or "正常"),
            source="手工登记",
            metric_name=(metric.get("指标名称") if metric else metric_name or None),
            evidence=(f"手工登记声明指标「{metric_name}」" if metric else ""),
            source_batch="manual",
        )
        entry["查询时间"] = str(values.get("查询时间") or entry["查询时间"])
        if metric is None:
            entry["unclaimed_reason"] = (f"手工登记未提供指标「{metric_name}」的可核实依据"
                                         if metric_name else "手工登记缺少指标身份")
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """状态流转直接落在执行账本上，列表、明细和动作读同一笔执行。"""
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        with store.lock:
            entry = store._executions.get(int(entry_id))
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            entry["status"] = target
            entry["pending"] = target != "正常"
            entry["abnormal"] = target == "慢查询"
            return dict(entry), f"{self.ENTITY}已{action}"

    def get_slow_queries(self, threshold_ms: float = 100) -> list[dict[str, Any]]:
        """获取慢查询列表。"""
        return [log for log in store.execution_snapshot()
                if log.get("duration_ms", 0) > threshold_ms]

    def _metrics_from_snapshot(self, snapshot: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """按当前有效明细算每个指标：次数、总耗时、平均耗时。

        “当前有效明细”= 快照中已归属、未被替代的执行，一笔只算一次。
        """
        by_metric: dict[str, dict[str, Any]] = {}
        for log in snapshot:
            if log.get("superseded"):
                continue
            if log.get("attribution") != "已归属" or log.get("metric_name") is None:
                continue
            metric_key = str(log["metric_name"])
            stats = by_metric.setdefault(metric_key, {
                "metric_id": log.get("metric_id"),
                "metric_name": metric_key,
                "count": 0,
                "slow": 0,
                "cache_hits": 0,
                "total_duration_ms": 0.0,
                "template_ids": [],
            })
            stats["count"] += 1
            stats["total_duration_ms"] += float(log.get("duration_ms", 0) or 0)
            if log.get("status") == "慢查询":
                stats["slow"] += 1
            if log.get("source") == "缓存命中":
                stats["cache_hits"] += 1
            template_id = log.get("template_id")
            if template_id is not None and template_id not in stats["template_ids"]:
                stats["template_ids"].append(template_id)

        for stats in by_metric.values():
            stats["avg_duration_ms"] = round(
                stats["total_duration_ms"] / max(1, stats["count"]), 2)
            del stats["total_duration_ms"]
        return by_metric

    def get_query_stats_by_metric(self) -> dict[str, Any]:
        """按指标统计查询情况（只含能证明身份的执行）。"""
        return self._metrics_from_snapshot(store.execution_snapshot())

    def get_query_detail(self) -> dict[str, Any]:
        """查询工作台用的指标查询明细：各指标次数、平均耗时、待认领清单。

        待认领不并入任何指标，但保留执行号，方便值班员补录后并回。
        """
        snapshot = store.execution_snapshot()
        by_metric = self._metrics_from_snapshot(snapshot)
        unclaimed = [row for row in snapshot
                     if row.get("attribution") == UNCLAIMED and not row.get("superseded")]
        unclaimed_rows = [{
            "execution_id": row.get("execution_id"),
            "查询时间": row.get("查询时间"),
            "操作人": row.get("操作人"),
            "查询语句摘要": row.get("查询语句摘要"),
            "template_id": row.get("template_id"),
            "source": row.get("source"),
            "source_batch": row.get("source_batch"),
            "legacy": row.get("legacy", False),
            "unclaimed_reason": row.get("unclaimed_reason", ""),
        } for row in unclaimed]
        return {
            "execution_count": len(snapshot),
            "attributed_count": sum(1 for row in snapshot if row.get("attribution") == "已归属"),
            "unclaimed_count": len(unclaimed_rows),
            "metrics": sorted(by_metric.values(), key=lambda item: str(item["metric_name"])),
            "unclaimed": unclaimed_rows,
        }

    def claim_execution(self, execution_id: int, evidence: str,
                        metric_id: int | None = None,
                        metric_name: str | None = None,
                        template_id: int | None = None,
                        operator: str = "值班员") -> dict[str, Any]:
        """值班员补录一条旧执行的归属依据；补录后该执行并回对应指标。"""
        entry, message = store.claim_execution(
            execution_id, evidence=evidence, metric_id=metric_id,
            metric_name=metric_name, template_id=template_id, operator=operator,
        )
        return {"ok": entry is not None, "message": message or "归属依据已补录",
                "entry": entry}

    def get_performance_insights(self) -> dict[str, Any]:
        """获取性能洞察：跨模块查询效率分析。"""
        logs = store.execution_snapshot()
        if not logs:
            return {"message": "暂无查询日志"}

        total = len(logs)
        slow = sum(1 for log in logs if log.get("status") == "慢查询")
        cached = sum(1 for log in logs if log.get("source") == "缓存命中")
        errors = sum(1 for log in logs if log.get("status") == "异常终止")
        unclaimed = sum(1 for log in logs if log.get("attribution") == UNCLAIMED)

        return {
            "total_queries": total,
            "slow_query_ratio": round(slow / max(1, total), 2),
            "cache_hit_ratio": round(cached / max(1, total), 2),
            "error_count": errors,
            "unclaimed_count": unclaimed,
            "recommendations": [
                f"{slow} 条慢查询建议优化口径或增加缓存" if slow > 0 else None,
                f"{cached} 条缓存命中，减少重复计算" if cached > 0 else "暂无缓存命中" if total > 0 else None,
                f"{unclaimed} 条待认领执行需要补录归属依据" if unclaimed > 0 else None,
            ]
        }
