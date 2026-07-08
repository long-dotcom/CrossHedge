export type V2NodeStatus = 'active' | 'blocked' | 'warning' | 'inactive';
export type V2Direction = 'long_leg_b_short_leg_a' | 'long_leg_a_short_leg_b';
export type V2HedgeStatus = 'holding' | 'closable' | 'manual' | 'building' | 'closing';

export type V2PipelineSymbol = {
  symbol: string;
  direction: V2Direction;
  leg_a_venue?: string;
  leg_a_symbol?: string;
  leg_b_venue?: string;
  leg_b_symbol?: string;
  spread: number;
  pipelineStatus: 'normal' | 'blocked';
  blockReason?: string;
  nodes: {
    sync: V2NodeStatus;
    scan: V2NodeStatus;
    signal: V2NodeStatus;
    candidate: V2NodeStatus;
  };
  delays: {
    hlToSync: number;
    mt5ToSync: number;
    syncToScan: number;
    scanToSignal: number;
    signalToCandidate: number;
  };
  timings: {
    scan: number;
    cost: number;
    signal: number;
    persist: number;
    resultAge: number;
  };
  netPnl?: number;
  annualized?: number;
};

export type V2HedgeGroup = {
  id: number;
  symbol: string;
  status: V2HedgeStatus;
  sortStage: string;
  pnl?: number;
  triggerSpread?: number;
  entrySpread?: number;
  currentSpread?: number;
};

export type V2LifecycleCounts = {
  pending: number;
  building: number;
  holding: number;
  closable: number;
  closing: number;
  abnormal: number;
};

export type V2DashboardData = {
  sseStatus: {
    online: boolean;
    latency: number;
    lastPush: number;
    enabledSymbols: number;
    normalFlow: number;
    blockedFlow: number;
  };
  pipelines: V2PipelineSymbol[];
  hedgeGroups: V2HedgeGroup[];
  lifecycle: V2LifecycleCounts;
  stats: {
    totalHedgeGroups: number;
    usedMargin: number;
    floatingPnl: number;
    todayClosed: number;
    todayReleased: number;
  };
  releasedCount: number;
  archivedCount: number;
};
