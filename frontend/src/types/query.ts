export interface QueryListItem {
  id: number
  external_id: string
  title: string
  source: string
  submitted_by: string
  query_date: string
  duration_ms: number
  is_scheduled: boolean
  parse_status: 'parsed' | 'unparsed' | 'missing_table'
  target_table: string | null
  prunable: boolean
  blocked_reason: string
  scan_bytes: number
  scan_cost_cny: number
  hit_partition_count: number
  total_partition_count: number
  col_ratio: number
  or_group_count: number
}

export interface Predicate {
  expression: string
  operator: string
  literal: string
  usable: boolean
  reason: string
  wrapper_kind: string | null
}

export interface OrBranch {
  index: number
  expression: string
  touches_partition_key: boolean
  prunable: boolean
  blocked_reason: string
  blocked_expression: string | null
  predicates: Predicate[]
  hit_partitions: string[]
  hit_partition_count: number
  hit_bytes: number
  scan_bytes: number
  cost_cny: number
  note: string
}

export interface OrGroup {
  index: number
  expression: string
  prunable: boolean
  blocked_reason: string
  branches: OrBranch[]
  union_partitions: string[]
  union_partition_count: number
  union_bytes: number
  scan_bytes: number
  cost_cny: number
  note: string
}

export interface QueryDetail extends QueryListItem {
  sql_text: string
  parse_error: string | null
  blocked_expression: string | null
  predicates: Predicate[]
  or_groups: OrGroup[]
  hit_partitions: string[]
  projected_columns: string[]
  referenced_columns: string[]
  has_star: boolean
  full_scan_bytes: number
  rationale: string
  suggestion_count: number
}

export interface QueryImportResult {
  imported: number
  updated: number
  total: number
  analysed: number
  blocked: number
  message: string
}

export interface QueryStats {
  query_count: number
  blocked_count: number
  prunable_count: number
  total_scan_bytes: number
  top_scan_bytes: number
  top_query_id: number | null
}
