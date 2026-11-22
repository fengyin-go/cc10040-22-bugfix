"""查询日志归属核账测试。

覆盖场景：甲指标两份模板（其中一份与乙指标同号）、旧日志里一条无法证明来源的执行。
分别冷跑、命中缓存，并在记录新调用的同时补录旧执行，核对：
  * 甲乙的次数、平均耗时；
  * 待认领清单；
  * 同一笔执行归并前后可追、只入账一次；
  * 旧模板 id 冒充指标归属不残留。
"""
from __future__ import annotations

import pytest

from app.services.query_builder import QueryBuilderService
from app.services.query_journal import QueryJournalService
from app.store import UNCLAIMED, store

METRIC_A = "甲指标"
METRIC_B = "乙指标"
TEMPLATE_A1 = 101
TEMPLATE_A2 = 102  # 与乙指标 id=102 同号
TEMPLATE_B = 103
LEGACY_EXECUTION = 901


@pytest.fixture()
def world():
    """每个用例从干净的种子数据开始，并补一条乙指标模板。"""
    with store.lock:
        store._executions.clear()
        store._cache.clear()
        store._exec_cursor = 0
        store.ingest_legacy_journal()
    if store.find("query_builder", TEMPLATE_B) is None:
        store.rows("query_builder").append({
            "id": TEMPLATE_B, "模板名称": "乙指标-日汇总模板", "关联指标": METRIC_B,
            "维度组合": "业务日期", "过滤条件": "", "缓存策略": "短期",
            "status": "已发布", "pending": False, "abnormal": False,
        })
    return store


def stat_for(detail, metric_name):
    return next((item for item in detail["metrics"] if item["metric_name"] == metric_name), None)


def test_cold_and_cache_calls_land_on_real_metrics(world):
    builder = QueryBuilderService()
    assert builder.execute_query(TEMPLATE_A1, operator="值班A")["source"] == "computed"
    assert builder.execute_query(TEMPLATE_A1, operator="值班A")["source"] == "cache"

    journal = QueryJournalService()
    detail = journal.get_query_detail()

    stat_a = stat_for(detail, METRIC_A)
    assert stat_a is not None
    assert stat_a["count"] == 2
    assert stat_a["cache_hits"] == 1
    # 同号模板 102 也是一笔甲指标的执行，但不会被算到乙指标头上。
    assert stat_for(detail, METRIC_B) is None

    # 乙指标单独执行一次，只算乙。
    builder.execute_query(TEMPLATE_B, operator="值班B")
    detail = journal.get_query_detail()
    assert stat_for(detail, METRIC_B)["count"] == 1
    assert stat_for(detail, METRIC_A)["count"] == 2


def test_same_numbered_template_cannot_stand_in_for_metric(world):
    builder = QueryBuilderService()
    builder.execute_query(TEMPLATE_A2, operator="值班C")
    detail = QueryJournalService().get_query_detail()

    # 模板 102 归甲指标；乙指标次数仍为 0，不会被“同号”冒领。
    assert stat_for(detail, METRIC_A)["count"] == 1
    assert stat_for(detail, METRIC_A)["template_ids"] == [TEMPLATE_A2]
    assert stat_for(detail, METRIC_B) is None

    # 显式用模板 102 冒充乙指标会被拒绝，并落为待认领。
    entry = store.record_execution(
        operator="错账", sql_summary="模板102冒充乙指标", row_count=1,
        duration_ms=5, template_id=TEMPLATE_A2, metric_name=METRIC_B,
    )
    assert entry["attribution"] == UNCLAIMED
    assert entry["metric_name"] is None
    assert stat_for(QueryJournalService().get_query_detail(), METRIC_B) is None


def test_unclaimed_legacy_stays_out_until_backfilled(world):
    journal = QueryJournalService()
    builder = QueryBuilderService()

    before = journal.get_query_detail()
    assert before["unclaimed_count"] >= 1
    legacy = next(row for row in before["unclaimed"]
                  if row["execution_id"] == LEGACY_EXECUTION)
    assert legacy["legacy"] is True
    assert stat_for(before, METRIC_A) is None

    # 执行过程中补录：先写一笔新调用，再补旧执行，统计按当前有效明细重算。
    builder.execute_query(TEMPLATE_A1, operator="值班A")
    claim = journal.claim_execution(
        LEGACY_EXECUTION,
        evidence="旧版构建器日志的 102 号模板即甲指标-同号模板，批次 LEGACY-20261020",
        template_id=TEMPLATE_A2,
        operator="值班员",
    )
    assert claim["ok"] is True

    after = journal.get_query_detail()
    stat_a = stat_for(after, METRIC_A)
    assert stat_a["count"] == 2
    assert after["unclaimed_count"] == before["unclaimed_count"] - 1

    # 同一笔执行归并前后 id 不变，可继续追到。
    assert store.find_execution(LEGACY_EXECUTION)["attribution"] == "已归属"
    assert store.find_execution(LEGACY_EXECUTION)["metric_name"] == METRIC_A

    # 一笔只入账一次：重复补录被拒绝，统计不再增加。
    repeat = journal.claim_execution(LEGACY_EXECUTION, evidence="重复", template_id=TEMPLATE_A2)
    assert repeat["ok"] is False
    assert stat_for(journal.get_query_detail(), METRIC_A)["count"] == 2


def test_claim_requires_verifiable_evidence(world):
    journal = QueryJournalService()
    assert journal.claim_execution(LEGACY_EXECUTION, evidence="")["ok"] is False
    # 只给“同号”的数字、不给模板/指标依据，不能把旧执行并到乙指标。
    bad = journal.claim_execution(LEGACY_EXECUTION, evidence="大概是乙指标", metric_id=102,
                                  template_id=TEMPLATE_A2)
    assert bad["ok"] is False
    assert store.find_execution(LEGACY_EXECUTION)["attribution"] == UNCLAIMED


def test_stats_snapshot_is_stable_while_new_calls_arrive(world):
    journal = QueryJournalService()
    builder = QueryBuilderService()
    builder.execute_query(TEMPLATE_A1, operator="值班A")

    snap = store.execution_snapshot()
    ids = [row["execution_id"] for row in snap]
    builder.execute_query(TEMPLATE_B, operator="值班B")
    # 已取到的快照不会随后续写入变化，同一轮统计口径稳定。
    assert [row["execution_id"] for row in snap] == ids
    assert len(store.execution_snapshot()) == len(snap) + 1


def test_legacy_log_query_never_guesses_identity(world):
    """旧的 log_query 只带编号时不能因为同号就归到乙指标。"""
    entry = store.log_query(102, operator="旧调用方", sql_summary="旧入口",
                            row_count=1, duration_ms=3)
    assert entry["attribution"] == UNCLAIMED
    assert entry["metric_name"] is None
    detail = QueryJournalService().get_query_detail()
    assert stat_for(detail, METRIC_B) is None
    assert any(row["execution_id"] == entry["execution_id"] for row in detail["unclaimed"])


def test_legacy_ingest_is_idempotent(world):
    before = len(store.execution_snapshot())
    assert store.ingest_legacy_journal() == 0
    assert len(store.execution_snapshot()) == before


def test_journal_list_matches_stats_source(world):
    builder = QueryBuilderService()
    builder.execute_query(TEMPLATE_A1, operator="值班A")
    builder.execute_query(TEMPLATE_A2, operator="值班B")
    journal = QueryJournalService()
    items, total = journal.list_entries(size=1000)
    assert total == len(store.execution_snapshot())
    assert sum(1 for row in items if row["attribution"] == "已归属") == 2
    # 旧执行与新执行同在一份明细里，不再拆成两组。
    assert any(row["execution_id"] == LEGACY_EXECUTION for row in items)


def test_concurrent_calls_and_claim_account_once(world):
    """边写新调用边补录旧执行、同轮刷新：执行号不重号，一笔只入账一次。"""
    import threading

    builder = QueryBuilderService()
    journal = QueryJournalService()
    builder.execute_query(TEMPLATE_A1, operator="底账")
    base = stat_for(journal.get_query_detail(), METRIC_A)["count"]
    errors: list[str] = []

    def write_calls(prefix: str) -> None:
        for _ in range(25):
            try:
                builder.execute_query(TEMPLATE_A1, operator=prefix)
            except Exception as exc:  # pragma: no cover - 失败时给出可读信息
                errors.append(str(exc))

    def read_stats() -> None:
        for _ in range(25):
            detail = journal.get_query_detail()
            ids = [row["execution_id"] for row in journal.list_entries(size=10_000)[0]]
            if len(ids) != len(set(ids)):
                errors.append("执行号重复入账")

    threads = [threading.Thread(target=write_calls, args=(f"并发{i}",)) for i in range(4)]
    threads += [threading.Thread(target=read_stats) for _ in range(2)]
    threads.append(threading.Thread(target=journal.claim_execution, args=(
        LEGACY_EXECUTION,), kwargs={"evidence": "并发补录", "template_id": TEMPLATE_A2}))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    detail = journal.get_query_detail()
    stat_a = stat_for(detail, METRIC_A)
    # 4 个线程 × 25 次新调用 + 底账 1 次 + 补录并回的旧执行 1 次。
    assert stat_a["count"] == base + 100 + 1
    ids = [row["execution_id"] for row in journal.list_entries(size=10_000)[0]]
    assert len(ids) == len(set(ids))
