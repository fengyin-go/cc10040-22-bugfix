<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <section v-if="meta.key === 'query_journal'" class="ledger" data-testid="metric-ledger">
      <header class="ledger-head">
        <div>
          <h3>指标查询明细</h3>
          <p class="page-desc">
            只把能证明指标归属的执行计入次数与平均耗时；同号模板不替代指标，待认领单列。
          </p>
        </div>
        <button class="btn" data-testid="reload-detail" type="button" @click="reloadDetail">
          刷新明细
        </button>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>指标</th>
            <th>调用次数</th>
            <th>平均耗时(ms)</th>
            <th>缓存命中</th>
            <th>慢查询</th>
            <th>来源模板</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="stat in queryDetail.metrics"
            :key="stat.metric_name"
            :data-testid="`metric-stat-${stat.metric_name}`"
          >
            <td>{{ stat.metric_name }}</td>
            <td data-testid="metric-count">{{ stat.count }}</td>
            <td data-testid="metric-avg">{{ stat.avg_duration_ms }}</td>
            <td>{{ stat.cache_hits }}</td>
            <td>{{ stat.slow }}</td>
            <td>{{ stat.template_ids.map((id) => `#${id}`).join('、') || '—' }}</td>
          </tr>
          <tr v-if="!queryDetail.metrics.length">
            <td colspan="6" class="empty-state">暂无已归属的指标执行</td>
          </tr>
        </tbody>
      </table>

      <h4>待认领执行（{{ queryDetail.unclaimed_count }}）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>执行号</th>
            <th>操作人</th>
            <th>查询摘要</th>
            <th>来源</th>
            <th>待认领原因</th>
            <th>补录归属依据</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in queryDetail.unclaimed"
            :key="item.execution_id"
            :data-testid="`unclaimed-${item.execution_id}`"
          >
            <td>{{ item.execution_id }}</td>
            <td>{{ item['操作人'] ?? '—' }}</td>
            <td>{{ item['查询语句摘要'] ?? '—' }}</td>
            <td>{{ item.source }}</td>
            <td>{{ item.unclaimed_reason }}</td>
            <td class="row-actions claim-cell">
              <input
                v-model="claimDraft[item.execution_id].metricName"
                :data-testid="`claim-metric-${item.execution_id}`"
                placeholder="归属指标名称"
              />
              <input
                v-model="claimDraft[item.execution_id].templateId"
                :data-testid="`claim-template-${item.execution_id}`"
                placeholder="模板编号(可选)"
              />
              <input
                v-model="claimDraft[item.execution_id].evidence"
                :data-testid="`claim-evidence-${item.execution_id}`"
                placeholder="填写可核实的归属依据"
              />
              <button
                class="link"
                type="button"
                :data-testid="`claim-${item.execution_id}`"
                @click="submitClaim(item.execution_id)"
              >
                补录并并回
              </button>
            </td>
          </tr>
          <tr v-if="!queryDetail.unclaimed.length">
            <td colspan="6" class="empty-state">没有待认领执行</td>
          </tr>
        </tbody>
      </table>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  claimExecution,
  createEntry,
  downloadEntries,
  listEntries,
  loadQueryDetail,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow, QueryDetail } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const isJournal = meta.key === 'query_journal'
const queryDetail = ref<QueryDetail>({
  execution_count: 0,
  attributed_count: 0,
  unclaimed_count: 0,
  metrics: [],
  unclaimed: [],
})
const claimDraft = ref<Record<number, { metricName: string; templateId: string; evidence: string }>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
  if (isJournal) await reloadDetail()
}

async function reloadDetail() {
  if (!isJournal) return
  try {
    queryDetail.value = await loadQueryDetail()
    for (const item of queryDetail.value.unclaimed) {
      claimDraft.value[item.execution_id] ??= { metricName: '', templateId: '', evidence: '' }
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标查询明细读取失败'
  }
}

async function submitClaim(executionId: number) {
  errorMessage.value = ''
  const draft = claimDraft.value[executionId]
  const evidence = (draft?.evidence ?? '').trim()
  if (!evidence) {
    errorMessage.value = '补录必须填写可核实的归属依据'
    return
  }
  const templateRaw = (draft?.templateId ?? '').trim()
  const payload: { evidence: string; metric_name?: string; template_id?: number } = { evidence }
  if ((draft?.metricName ?? '').trim()) payload.metric_name = draft.metricName.trim()
  if (templateRaw && /^\d+$/.test(templateRaw)) payload.template_id = Number(templateRaw)
  try {
    const result = await claimExecution(executionId, payload)
    if (!result.ok) throw new Error(result.message)
    claimDraft.value[executionId] = { metricName: '', templateId: '', evidence: '' }
    // 补录后日志列表和明细都要按当前有效明细重算，保证同一笔执行只入账一次。
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归属补录失败'
  }
}

onMounted(reload)
</script>
