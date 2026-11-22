"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
同时提供跨模块查询和数据流处理能力。
"""
from __future__ import annotations

import time
import threading
from typing import Any

from app.seed import SEED_ROWS

# 无法证明归属的执行统一归到待认领，不混入任何指标。
UNCLAIMED = "待认领"


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._id_counters: dict[str, int] = {}
        # Cache layer storage
        self._cache: dict[str, tuple[float, Any]] = {}
        # 执行账本：每一笔真实执行按执行号（execution_id）只记一次。
        # _executions 是唯一事实源，列表、统计、补录、动作都读它。
        self._executions: dict[int, dict[str, Any]] = {}
        self._exec_cursor: int = 0
        # Cohort snapshots
        self._cohort_snapshots: dict[str, list[dict[str, Any]]] = {}
        # Anomaly records
        self._anomaly_records: list[dict[str, Any]] = []
        # Recalc history
        self._recalc_history: list[dict[str, Any]] = []
        # Lock for thread safety
        self._lock = threading.Lock()

    @property
    def lock(self):
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        if not rows:
            return 1
        return max(int(row.get("id", 0)) for row in rows) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ── Cross-module query helpers ──

    def find_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """查找模块中 field 等于 value 的所有记录。"""
        return [row for row in self.rows(module) if row.get(field) == value]

    def find_by_status(self, module: str, status: str) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get("status") == status]

    # ── 执行账本 / 查询日志 ──
    #
    # 一笔执行 = 一个 execution_id，从产生到补录归属都只入账一次。
    # 查询日志的列表和统计都读这本账，避免“日志列表拆成两组”。
    # 归属只认能证明的指标身份：
    #   * 模板身份（query_builder.id）与指标身份（metric_definition.id）分开存，
    #     同号时绝不互相替代；
    #   * 模板 -> “关联指标”名称 -> 指标记录 才是可核实的链路；
    #   * 证明不了的一律 UNCLAIMED，留在待认领，不进入任何指标统计。

    def _metric_by_name(self, metric_name: str) -> dict[str, Any] | None:
        for row in self.rows("metric_definition"):
            if row.get("指标名称") == metric_name:
                return row
        return None

    def _resolve_template(self, template_id: int | None) -> dict[str, Any] | None:
        if template_id is None:
            return None
        return self.find("query_builder", int(template_id))

    def _proven_identity(self, template_id: int | None,
                         metric_id: int | None,
                         metric_name: str | None) -> tuple[dict[str, Any] | None, str]:
        """返回 (证据化身份, 证据说明)；无法证明时返回 (None, 原因)。

        优先用实际模板反查关联指标，这是执行时能留下的事实。
        没有模板时，只接受能直接落到指标定义上的指标 ID/名称，且要求两者一致，
        防止把同号的模板 ID 当成指标 ID。
        """
        template = self._resolve_template(template_id)
        if template is not None:
            linked_name = template.get("关联指标", "")
            metric = self._metric_by_name(linked_name)
            if metric is None:
                return None, f"模板#{template_id}关联的指标「{linked_name}」不存在"
            if metric_name and metric_name != linked_name:
                return None, f"模板#{template_id}关联「{linked_name}」，与声明的「{metric_name}」不符"
            if metric_id is not None and int(metric_id) != int(metric["id"]):
                return None, (f"模板#{template_id}关联指标#{metric['id']}，"
                              f"与声明的指标#{metric_id}不符")
            return {
                "metric_id": int(metric["id"]),
                "metric_name": metric["指标名称"],
                "template_id": int(template["id"]),
                "template_name": template.get("模板名称", ""),
                "evidence": f"模板#{template['id']}→关联指标「{metric['指标名称']}」",
            }, ""
        if metric_name:
            metric = self._metric_by_name(metric_name)
            if metric is None:
                return None, f"指标「{metric_name}」不存在"
            if metric_id is not None and int(metric_id) != int(metric["id"]):
                return None, f"指标「{metric_name}」的编号是#{metric['id']}，与声明的 #{metric_id} 不符"
            return {
                "metric_id": int(metric["id"]),
                "metric_name": metric["指标名称"],
                "template_id": None,
                "template_name": None,
                "evidence": f"指标定义#{metric['id']}「{metric['指标名称']}」",
            }, ""
        if metric_id is not None:
            metric = self.find("metric_definition", int(metric_id))
            if metric is None:
                return None, f"指标#{metric_id}不存在；单独的编号不能证明归属"
            return {
                "metric_id": int(metric["id"]),
                "metric_name": metric["指标名称"],
                "template_id": None,
                "template_name": None,
                "evidence": f"指标定义#{metric['id']}「{metric['指标名称']}」",
            }, ""
        return None, "没有可核实的模板或指标身份"

    def _make_execution(self, *, operator: str, sql_summary: str, row_count: int,
                        duration_ms: float, status: str, source: str,
                        template_id: int | None = None, metric_id: int | None = None,
                        metric_name: str | None = None, evidence: str = "",
                        source_batch: str = "live", legacy: bool = False,
                        claimed_by: str | None = None) -> dict[str, Any]:
        identity, reason = self._proven_identity(template_id, metric_id, metric_name)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        unclaimed = identity is None
        entry = {
            "id": 0,
            "execution_id": 0,
            "查询时间": now,
            "操作人": operator,
            "涉及指标": UNCLAIMED if unclaimed else identity["metric_name"],
            "查询语句摘要": sql_summary,
            "返回行数": str(row_count),
            "status": status,
            "duration_ms": round(float(duration_ms), 2),
            "pending": status != "正常",
            "abnormal": status in ("慢查询", "异常终止"),
            "source": source,
            "source_batch": source_batch,
            "legacy": legacy,
            "attribution": UNCLAIMED if unclaimed else "已归属",
            "metric_id": identity["metric_id"] if identity else None,
            "metric_name": identity["metric_name"] if identity else None,
            "template_id": identity["template_id"] if identity else template_id,
            "template_name": (identity["template_name"] if identity else None),
            "evidence": (identity["evidence"] if identity else evidence),
            "unclaimed_reason": reason if unclaimed else "",
            "claimed_at": now if unclaimed and legacy else None,
            "claimed_by": claimed_by if unclaimed else None,
        }
        return entry

    def _append_execution(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._exec_cursor += 1
            entry["execution_id"] = self._exec_cursor
            entry["id"] = self._exec_cursor
            self._executions[self._exec_cursor] = entry
            return dict(entry)

    def record_execution(self, *, operator: str, sql_summary: str, row_count: int,
                         duration_ms: float, status: str = "正常", source: str = "冷执行",
                         template_id: int | None = None, metric_id: int | None = None,
                         metric_name: str | None = None, evidence: str = "",
                         source_batch: str = "live") -> dict[str, Any]:
        """记录一笔实际执行；冷执行和缓存命中都要落账，身份取自实际模板。"""
        entry = self._make_execution(
            operator=operator, sql_summary=sql_summary, row_count=row_count,
            duration_ms=duration_ms, status=status, source=source,
            template_id=template_id, metric_id=metric_id, metric_name=metric_name,
            evidence=evidence, source_batch=source_batch,
        )
        return self._append_execution(entry)

    def log_query(self, metric_id: int, operator: str, sql_summary: str,
                  row_count: int, duration_ms: float, status: str = "正常") -> dict[str, Any]:
        """旧调用方的兼容入口：一律进待认领，不按编号猜归属。

        旧实现把模板编号当指标编号传进来，同一个数字既可能是模板也可能是指标；
        仅凭编号无法证明这是哪一边的身份，更不能因为“同号”就把执行记到别的指标上。
        这类执行会在待认领里保留原始编号，等值班员补录可核实的依据后再并回。
        """
        entry = self.record_execution(
            operator=operator, sql_summary=sql_summary, row_count=row_count,
            duration_ms=duration_ms, status=status, source="冷执行",
            evidence=f"旧的 metric_{metric_id} 标记，无法证明是模板还是指标",
            source_batch="legacy",
        )
        with self._lock:
            entry = self._executions[entry["execution_id"]]
            entry["template_id"] = int(metric_id) if str(metric_id).isdigit() else None
            entry["unclaimed_reason"] = (
                f"旧日志只有 metric_{metric_id} 标记，模板与指标同号时无法证明归属")
            return dict(entry)

    def execution_snapshot(self) -> list[dict[str, Any]]:
        """同一轮统计取同一份快照：不会把半批新执行混进来。"""
        with self._lock:
            return [dict(entry) for entry in
                    sorted(self._executions.values(), key=lambda e: e["execution_id"])]

    def query_logs(self) -> list[dict[str, Any]]:
        return self.execution_snapshot()

    def find_execution(self, execution_id: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self._executions.get(int(execution_id))
            return dict(entry) if entry is not None else None

    def claim_execution(self, execution_id: int, *, evidence: str,
                        metric_id: int | None = None, metric_name: str | None = None,
                        template_id: int | None = None,
                        operator: str = "值班员") -> tuple[dict[str, Any] | None, str]:
        """给待认领执行补录可核实的归属依据；已归属的不会再入账一次。"""
        if not str(evidence or "").strip():
            return None, "补录必须填写可核实的归属依据"
        with self._lock:
            entry = self._executions.get(int(execution_id))
            if entry is None:
                return None, f"执行 {execution_id} 不存在"
            if entry.get("attribution") != UNCLAIMED:
                return None, (f"执行 {execution_id} 已归属指标「{entry.get('metric_name')}」，"
                              "一笔执行只入账一次")
            identity, reason = self._proven_identity(template_id, metric_id, metric_name)
            if identity is None:
                return None, f"归属依据不足：{reason}"
            entry.update({
                "涉及指标": identity["metric_name"],
                "attribution": "已归属",
                "metric_id": identity["metric_id"],
                "metric_name": identity["metric_name"],
                "template_id": identity["template_id"] or entry.get("template_id"),
                "template_name": identity["template_name"] or entry.get("template_name"),
                "evidence": str(evidence).strip(),
                "unclaimed_reason": "",
                "claimed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "claimed_by": operator,
            })
            return dict(entry), ""

    def ingest_legacy_journal(self) -> int:
        """把仓库里既有的查询日志并入执行账本，保留原编号以便归并前后追同一笔。

        幂等：重复调用不会把同一笔旧执行再入账一次。
        """
        ingested = 0
        for row in self.rows("query_journal"):
            execution_id = int(row.get("id", 0)) or (max(self._executions, default=0) + 1)
            if execution_id in self._executions:
                # 同号的旧日志已经入账，按“一笔只入账一次”跳过。
                continue
            entry = self._make_execution(
                operator=str(row.get("操作人", "旧日志")),
                sql_summary=str(row.get("查询语句摘要", "")),
                row_count=_as_int(row.get("返回行数")),
                duration_ms=float(row.get("duration_ms", 0) or 0),
                status=str(row.get("status", "正常")),
                source=str(row.get("source", "旧日志")),
                template_id=row.get("template_id"),
                metric_id=row.get("metric_id"),
                metric_name=row.get("metric_name"),
                evidence=str(row.get("evidence", "")),
                source_batch=str(row.get("source_batch", "legacy")),
                legacy=True,
                claimed_by=row.get("claimed_by"),
            )
            entry["查询时间"] = str(row.get("查询时间", entry["查询时间"]))
            if row.get("attribution") == "已归属" and entry["attribution"] == UNCLAIMED:
                # 旧日志只有在带得动可核实身份时才保留归属，否则回落待认领。
                entry["unclaimed_reason"] = "旧日志没有可核实身份"
            entry["execution_id"] = execution_id
            entry["id"] = execution_id
            self._executions[execution_id] = entry
            self._exec_cursor = max(self._exec_cursor, execution_id)
            ingested += 1
        return ingested

    # ── Cache operations ──

    def cache_get(self, key: str) -> Any | None:
        """获取缓存，过期返回 None。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._cache[key]
            return None
        return value

    def cache_set(self, key: str, value: Any, ttl: int) -> None:
        """设置缓存，ttl 单位为秒。"""
        self._cache[key] = (time.time() + ttl, value)

    def cache_invalidate(self, key: str) -> bool:
        return self._cache.pop(key, None) is not None

    def cache_invalidate_by_prefix(self, prefix: str) -> int:
        count = 0
        for key in list(self._cache):
            if key.startswith(prefix):
                del self._cache[key]
                count += 1
        return count

    # ── Cohort operations ──

    def save_cohort_snapshot(self, cohort_id: int, members: list[dict[str, Any]]) -> None:
        self._cohort_snapshots[str(cohort_id)] = members

    def get_cohort_snapshot(self, cohort_id: int) -> list[dict[str, Any]] | None:
        return self._cohort_snapshots.get(str(cohort_id))

    # ── Anomaly detection operations ──

    def record_anomaly(self, metric_id: int, severity: str, description: str,
                       value: float, threshold: float) -> dict[str, Any]:
        record = {
            "id": len(self._anomaly_records) + 1,
            "指标ID": metric_id,
            "严重程度": severity,
            "描述": description,
            "当前值": value,
            "阈值": threshold,
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._anomaly_records.append(record)
        return record

    def anomaly_records(self) -> list[dict[str, Any]]:
        return self._anomaly_records

    # ── Recalc operations ──

    def record_recalc(self, metric_id: int, old_version: str, new_version: str,
                      time_range: str, diff_summary: str) -> dict[str, Any]:
        record = {
            "id": len(self._recalc_history) + 1,
            "关联指标": f"metric_{metric_id}",
            "旧口径": old_version,
            "新口径": new_version,
            "历史时间范围": time_range,
            "产出差异": diff_summary,
            "重算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._recalc_history.append(record)
        return record

    def recalc_history(self) -> list[dict[str, Any]]:
        return self._recalc_history

    # ── Data artifacts for cross-module consumption ──

    def get_metric_data(self, metric_id: int) -> dict[str, Any] | None:
        """Get metric data with its calibers, queries, and services."""
        metric = self.find("metric_definition", metric_id)
        if metric is None:
            return None
        return {
            "metric": metric,
            "calibers": self.find_by_field("caliber_version", "所属指标",
                                           metric.get("指标名称", "")),
            "queries": self.find_by_field("query_builder", "关联指标",
                                          metric.get("指标名称", "")),
            "services": [row for row in self.rows("data_service")
                         if metric.get("指标名称", "") in str(row.get("关联指标或模型", ""))],
            "subscriptions": self.find_by_field("data_subscription", "关联指标",
                                                 metric.get("指标名称", "")),
        }

    def get_dimension_model_data(self, model_id: int) -> dict[str, Any] | None:
        """Get dimension model with its drill-down paths and pivot configs."""
        model = self.find("dimension_model", model_id)
        if model is None:
            return None
        return {
            "model": model,
            "drill_paths": self.find_by_field("drill_down", "起始指标",
                                              model.get("模型名称", "")),
            "pivot_configs": self.find_by_field("pivot_table", "数据源指标",
                                                 model.get("模型名称", "")),
        }

    def get_cohort_analysis(self, cohort_id: int) -> dict[str, Any] | None:
        """Get cohort with experiments and segment comparisons."""
        cohort = self.find("user_cohort", cohort_id)
        if cohort is None:
            return None
        cohort_name = cohort.get("分群名称", "")
        return {
            "cohort": cohort,
            "experiments": [row for row in self.rows("experiment_group")
                            if cohort_name in str(row.get("筛选条件", ""))],
            "comparisons": [row for row in self.rows("segment_compare")
                            if cohort_name in (str(row.get("分群A", "")) + str(row.get("分群B", "")))],
            "anomaly_rules": [row for row in self.rows("anomaly_detector")
                              if cohort_name in str(row.get("目标指标", ""))],
        }

    def get_field_lineage(self, field_name: str) -> dict[str, Any]:
        """Trace a field's complete lineage chain."""
        entries = self.find_by_field("field_lineage", "目标字段", field_name)
        if not entries:
            return {"field": field_name, "lineage": [], "depth": 0}

        lineage = [{
            "source_fields": entry.get("来源字段列表", ""),
            "transform": entry.get("转换逻辑", ""),
            "intermediate_table": entry.get("中间表", ""),
            "depth": entry.get("血缘深度", ""),
            "status": entry.get("status", ""),
        } for entry in entries]

        return {
            "field": field_name,
            "lineage": lineage,
            "depth": len(lineage),
        }


store = Store()