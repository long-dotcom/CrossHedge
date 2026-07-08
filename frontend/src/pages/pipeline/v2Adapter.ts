import type { HedgePoolItem, PipelineDiagnostics, PipelineNode, PipelineStatus, SymbolPipeline } from './types';
import type { V2DashboardData, V2HedgeGroup, V2HedgeStatus, V2NodeStatus, V2PipelineSymbol } from './v2Types';
import type { StreamStatus } from '../../hooks/useLiveStream';

function toNodeStatus(status?: PipelineStatus): V2NodeStatus {
  if (status === 'flowing' || status === 'pass') return 'active';
  if (status === 'blocked') return 'blocked';
  if (status === 'warning') return 'warning';
  return 'inactive';
}

function getNode(symbol: SymbolPipeline, key: string): PipelineNode | undefined {
  return symbol.nodes.find((node) => node.key === key);
}

function ms(value?: number | null): number {
  if (value === undefined || value === null || !Number.isFinite(value)) return 0;
  return Math.max(Math.round(value), 0);
}

function pipelineStatus(symbol: SymbolPipeline, key: string): V2NodeStatus {
  if (symbol.blocked_stage === key) return 'blocked';
  return toNodeStatus(getNode(symbol, key)?.status);
}

function toPipeline(symbol: SymbolPipeline): V2PipelineSymbol {
  const status = symbol.status === 'blocked' ? 'blocked' : 'normal';
  return {
    symbol: symbol.symbol,
    direction: symbol.metrics.gross_spread && symbol.metrics.gross_spread < 0 ? 'long_leg_a_short_leg_b' : 'long_leg_b_short_leg_a',
    leg_a_venue: symbol.leg_a_venue,
    leg_a_symbol: symbol.leg_a_symbol || symbol.leg_a_venue_symbol,
    leg_b_venue: symbol.leg_b_venue,
    leg_b_symbol: symbol.leg_b_symbol || symbol.mt5_symbol,
    spread: Number(symbol.metrics.unit_net_profit ?? symbol.metrics.gross_spread ?? 0),
    pipelineStatus: status,
    blockReason: symbol.reason,
    nodes: {
      sync: pipelineStatus(symbol, 'sync'),
      scan: pipelineStatus(symbol, 'scan'),
      signal: pipelineStatus(symbol, 'signal'),
      candidate: pipelineStatus(symbol, 'candidate'),
    },
    delays: {
      hlToSync: ms(symbol.metrics.leg_a_age_ms ?? symbol.metrics.hl_age_ms),
      mt5ToSync: ms(symbol.metrics.leg_b_age_ms ?? symbol.metrics.mt5_age_ms),
      syncToScan: ms(symbol.metrics.symbol_scan_duration_ms),
      scanToSignal: ms(symbol.metrics.signal_duration_ms),
      signalToCandidate: ms(symbol.metrics.candidate_sync_duration_ms),
    },
    timings: {
      scan: ms(symbol.metrics.symbol_scan_duration_ms),
      cost: ms(symbol.metrics.cost_duration_ms),
      signal: ms(symbol.metrics.signal_duration_ms),
      persist: ms(symbol.metrics.persist_duration_ms),
      resultAge: ms(symbol.metrics.scan_age_ms),
    },
    netPnl: Number(symbol.metrics.unit_net_profit ?? 0),
    annualized: Number(symbol.metrics.annualized_return ?? 0) * 100,
  };
}

function toHedgeStatus(item: HedgePoolItem): V2HedgeStatus {
  if (item.stage === 'ready_to_close') return 'closable';
  if (item.stage === 'opening' || item.stage === 'pending') return 'building';
  if (item.stage === 'closing') return 'closing';
  if (item.stage === 'manual') return 'manual';
  return 'holding';
}

function toHedgeGroup(item: HedgePoolItem): V2HedgeGroup {
  return {
    id: item.id,
    symbol: item.symbol,
    status: toHedgeStatus(item),
    sortStage: item.stage,
    pnl: Number(item.unrealized_pnl || 0),
    triggerSpread: item.trigger_spread == null ? undefined : Number(item.trigger_spread),
    entrySpread: Number(item.entry_spread || 0),
    currentSpread: item.current_close_spread == null ? undefined : Number(item.current_close_spread),
  };
}

const hedgeStageOrder: Record<string, number> = {
  pending: 0,
  opening: 1,
  open: 2,
  ready_to_close: 3,
  closing: 4,
  manual: 5,
};

function sortHedgeGroups(a: V2HedgeGroup, b: V2HedgeGroup): number {
  const stageDiff = (hedgeStageOrder[a.sortStage] ?? 99) - (hedgeStageOrder[b.sortStage] ?? 99);
  if (stageDiff !== 0) return stageDiff;
  const symbolDiff = a.symbol.localeCompare(b.symbol);
  if (symbolDiff !== 0) return symbolDiff;
  return a.id - b.id;
}

function laneCount(data: PipelineDiagnostics, key: string): number {
  return data.pool.lanes.find((lane) => lane.key === key)?.count || 0;
}

export function toV2DashboardData(data: PipelineDiagnostics, streamStatus?: StreamStatus): V2DashboardData {
  const hedgeGroups = data.pool.items.map(toHedgeGroup).sort(sortHedgeGroups);
  const floatingPnl = hedgeGroups.reduce((sum, item) => sum + Number(item.pnl || 0), 0);
  const usedMargin = data.pool.items.reduce((sum, item) => sum + Number(item.notional || 0), 0);
  return {
    sseStatus: {
      online: streamStatus?.online ?? false,
      latency: streamStatus?.latencySeconds ?? 0,
      lastPush: streamStatus?.lastPushSeconds ?? 0,
      enabledSymbols: data.summary.enabled_symbols,
      normalFlow: data.summary.flowing,
      blockedFlow: data.summary.blocked,
    },
    pipelines: data.symbols.map(toPipeline),
    hedgeGroups,
    lifecycle: {
      pending: laneCount(data, 'pending'),
      building: laneCount(data, 'opening'),
      holding: laneCount(data, 'open'),
      closable: laneCount(data, 'ready_to_close'),
      closing: laneCount(data, 'closing'),
      abnormal: laneCount(data, 'manual'),
    },
    stats: {
      totalHedgeGroups: data.pool.active_total,
      usedMargin,
      floatingPnl,
      todayClosed: data.summary.ready_to_close,
      todayReleased: 0,
    },
    releasedCount: data.summary.ready_to_close,
    archivedCount: 0,
  };
}
