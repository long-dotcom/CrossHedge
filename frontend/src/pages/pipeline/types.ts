export type PipelineStatus = 'flowing' | 'pass' | 'warning' | 'blocked' | 'idle' | string;

export type PipelineNode = {
  key: string;
  label: string;
  status: PipelineStatus;
  message: string;
  age_ms?: number | null;
  latency_ms?: number | null;
  source?: string;
  bid?: number | null;
  ask?: number | null;
  opportunity?: Record<string, unknown> | null;
};

export type PipelineEdge = {
  source: string;
  target: string;
  latency_ms?: number | null;
  status: PipelineStatus;
  label: string;
};

export type SymbolPipeline = {
  symbol: string;
  leg_a_venue_symbol: string;
  mt5_symbol: string;
  leg_a_venue?: string;
  leg_a_symbol?: string;
  leg_b_venue?: string;
  leg_b_symbol?: string;
  status: PipelineStatus;
  blocked_stage: string;
  reason: string;
  blockers?: Array<{ stage: string; message: string }>;
  nodes: PipelineNode[];
  edges: PipelineEdge[];
  metrics: {
    hl_age_ms?: number | null;
    mt5_age_ms?: number | null;
    leg_a_age_ms?: number | null;
    leg_b_age_ms?: number | null;
    sync_diff_ms?: number | null;
    scan_age_ms?: number | null;
    quote_sync_duration_ms?: number | null;
    symbol_scan_duration_ms?: number | null;
    sizing_duration_ms?: number | null;
    cost_duration_ms?: number | null;
    signal_duration_ms?: number | null;
    candidate_sync_duration_ms?: number | null;
    persist_duration_ms?: number | null;
    gross_spread?: number | null;
    unit_net_profit?: number | null;
    annualized_return?: number | null;
  };
};

export type HedgePoolItem = {
  id: number;
  symbol: string;
  direction: string;
  leg_a_venue?: string;
  leg_a_symbol?: string;
  leg_b_venue?: string;
  leg_b_symbol?: string;
  status: string;
  stage: string;
  stage_label: string;
  execution_mode: string;
  notional: number;
  quantity: number;
  trigger_spread?: number | null;
  entry_spread: number;
  current_entry_spread?: number | null;
  current_close_spread?: number | null;
  quote_time_diff_ms?: number | null;
  quote_age_ms?: number | null;
  exit_target: number;
  realized_pnl: number;
  unrealized_pnl: number;
  close_reason: string;
  age_ms?: number | null;
};

export type PipelineDiagnostics = {
  generated_at: string;
  summary: {
    enabled_symbols: number;
    flowing: number;
    blocked: number;
    warning: number;
    candidate: number;
    pool_active: number;
    ready_to_close: number;
  };
  symbols: SymbolPipeline[];
  pool: {
    active_total: number;
    lanes: Array<{ key: string; label: string; count: number }>;
    items: HedgePoolItem[];
  };
};
