/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

/** 指标查询明细：每个指标只统计能证明身份的执行，待认领单列。 */
export type MetricQueryStat = {
  metric_id: number | null
  metric_name: string
  count: number
  slow: number
  cache_hits: number
  avg_duration_ms: number
  template_ids: number[]
}

export type UnclaimedExecution = {
  execution_id: number
  template_id: number | null
  source: string
  legacy: boolean
  unclaimed_reason: string
  [field: string]: string | number | boolean | null
}

export type QueryDetail = {
  execution_count: number
  attributed_count: number
  unclaimed_count: number
  metrics: MetricQueryStat[]
  unclaimed: UnclaimedExecution[]
}
