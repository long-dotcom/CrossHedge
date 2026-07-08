import { fmtAdaptive, fmtSpread } from '../../utils/format';
import { directionLabel, venueLabel } from '../../utils/venues';
import type { V2DashboardData, V2HedgeGroup, V2NodeStatus, V2PipelineSymbol } from './v2Types';

function delayClass(ms: number) {
  if (ms === 0) return 'delay-idle';
  if (ms < 20) return 'delay-good';
  if (ms <= 100) return 'delay-warn';
  return 'delay-bad';
}

function statusClass(status: V2NodeStatus) {
  return `v2-node-${status}`;
}

function PipelineNode({ label, status }: { label: string; status: V2NodeStatus }) {
  return (
    <div className={`v2-pipeline-node ${statusClass(status)}`}>
      <span />
      <strong>{label}</strong>
    </div>
  );
}

function DelayBadge({ ms }: { ms: number }) {
  return <span className={`v2-delay ${delayClass(ms)}`}>{ms >= 0 ? `${ms}ms` : '-'}</span>;
}

function PipeSegment({ status }: { status: 'normal' | 'blocked' | 'inactive' }) {
  return (
    <svg className="v2-pipe-segment" viewBox="0 0 40 20" preserveAspectRatio="none">
      <rect x="0" y="8" width="40" height="4" rx="2" className={`v2-pipe-track pipe-${status}`} />
      <line x1="2" y1="10" x2="38" y2="10" className={`v2-pipe-flow pipe-${status}`} />
    </svg>
  );
}

function segmentStatus(from: V2NodeStatus, to: V2NodeStatus): 'normal' | 'blocked' | 'inactive' {
  if (from === 'inactive' || to === 'inactive') return 'inactive';
  if (from === 'blocked' || to === 'blocked') return 'blocked';
  return 'normal';
}

function PipelineRow({ data }: { data: V2PipelineSymbol }) {
  const isBlocked = data.pipelineStatus === 'blocked';
  const stages = [
    { key: 'scan' as const, label: '扫描', delay: data.delays.syncToScan },
    { key: 'signal' as const, label: '信号', delay: data.delays.scanToSignal },
    { key: 'candidate' as const, label: '候选', delay: data.delays.signalToCandidate },
  ];
  let previous: V2NodeStatus = data.nodes.sync;
  return (
    <div className={`v2-pipeline-row ${isBlocked ? 'is-blocked' : ''}`}>
      <div className="v2-row-header">
        <div className="v2-row-title">
          <strong>{data.symbol}</strong>
          <span>{directionLabel(data.direction, data)}</span>
        </div>
        <div className={`v2-spread ${data.spread >= 0 ? 'positive' : 'negative'}`}>价差 {data.spread >= 0 ? '+' : ''}{fmtSpread(data.spread)}</div>
      </div>
      <div className="v2-row-flow">
        <div className="v2-sources">
          <div className="v2-source hl"><i />{venueLabel(data.leg_a_venue)}</div>
          <div className="v2-source mt5"><i />{venueLabel(data.leg_b_venue)}</div>
        </div>
        <svg className="v2-merge-svg" viewBox="0 0 56 76">
          <line x1="0" y1="18" x2="24" y2="18" className={`v2-merge-line ${isBlocked ? 'blocked' : 'hl'}`} />
          <line x1="24" y1="18" x2="42" y2="38" className={`v2-merge-line ${isBlocked ? 'blocked' : 'hl'}`} />
          <text x="10" y="13" className={delayClass(data.delays.hlToSync)}>{data.delays.hlToSync}ms</text>
          <line x1="0" y1="58" x2="24" y2="58" className={`v2-merge-line ${isBlocked ? 'blocked' : 'mt5'}`} />
          <line x1="24" y1="58" x2="42" y2="38" className={`v2-merge-line ${isBlocked ? 'blocked' : 'mt5'}`} />
          <text x="10" y="54" className={delayClass(data.delays.mt5ToSync)}>{data.delays.mt5ToSync}ms</text>
          <line x1="42" y1="38" x2="56" y2="38" className={`v2-merge-line ${isBlocked ? 'blocked' : 'normal'}`} />
          <circle cx="42" cy="38" r="3.5" className={`v2-merge-dot ${isBlocked ? 'blocked' : 'normal'}`} />
        </svg>
        <PipelineNode label="同步" status={data.nodes.sync} />
        {stages.map((stage) => {
          const current = data.nodes[stage.key];
          const seg = segmentStatus(previous, current);
          previous = current;
          return (
            <div className="v2-stage-chain" key={stage.key}>
              <PipeSegment status={seg} />
              <DelayBadge ms={stage.delay} />
              <PipeSegment status={seg} />
              <PipelineNode label={stage.label} status={current} />
            </div>
          );
        })}
      </div>
      {isBlocked && data.blockReason && <div className="v2-block-reason">{data.blockReason}</div>}
      {data.netPnl !== undefined && data.annualized !== undefined && data.pipelineStatus === 'normal' && (
        <div className="v2-row-profit">
          <span>净利 <strong>{data.netPnl >= 0 ? '+' : ''}{fmtAdaptive(data.netPnl)}</strong></span>
          <span>年化 <strong>{data.annualized.toFixed(2)}%</strong></span>
        </div>
      )}
    </div>
  );
}

function PipelinePanelV2({ pipelines }: { pipelines: V2PipelineSymbol[] }) {
  return (
    <section className="v2-panel v2-pipeline-panel">
      <div className="v2-panel-header green">
        <h2>行情管道</h2>
        <span>按品种映射双腿同步</span>
      </div>
      <div className="v2-legend">
        <span><i className="normal" />正常流动</span>
        <span><i className="blocked" />阻塞</span>
        <span><i className="warning" />警告</span>
        <span><i className="inactive" />未激活</span>
      </div>
      <div className="v2-pipeline-list">
        {pipelines.map((pipeline) => <PipelineRow key={pipeline.symbol} data={pipeline} />)}
      </div>
    </section>
  );
}

const hedgeMeta: Record<V2HedgeGroup['status'], { label: string; cls: string }> = {
  holding: { label: '持仓中', cls: 'holding' },
  closable: { label: '可平仓', cls: 'closable' },
  manual: { label: '人工接管', cls: 'manual' },
  building: { label: '建仓中', cls: 'building' },
  closing: { label: '平仓中', cls: 'closing' },
};

const hedgeSections: Array<{ key: V2HedgeGroup['status']; label: string }> = [
  { key: 'building', label: '待执行 / 建仓中' },
  { key: 'holding', label: '持仓中' },
  { key: 'closable', label: '可平仓' },
  { key: 'closing', label: '平仓中' },
  { key: 'manual', label: '人工接管' },
];

function spreadLabel(value?: number) {
  return value === undefined ? '-' : fmtSpread(value);
}

function HedgeRow({ group }: { group: V2HedgeGroup }) {
  const meta = hedgeMeta[group.status];
  return (
    <div className={`v2-hedge-row ${meta.cls}`}>
      <div className="v2-hedge-main">
        <div className="v2-hedge-title"><strong>{group.symbol}</strong><span>#{group.id}</span></div>
        <div className="v2-hedge-status"><i />{meta.label}</div>
      </div>
      <div className="v2-hedge-spreads">
        <span>触发 <strong>{spreadLabel(group.triggerSpread)}</strong></span>
        <span>开仓 <strong>{spreadLabel(group.entrySpread)}</strong></span>
        <span>当前 <strong>{spreadLabel(group.currentSpread)}</strong></span>
      </div>
      <div className="v2-hedge-pnl">PnL <strong className={(group.pnl || 0) >= 0 ? 'positive' : 'negative'}>{(group.pnl || 0) >= 0 ? '+' : ''}{(group.pnl || 0).toFixed(2)}</strong></div>
    </div>
  );
}

function HedgePoolPanelV2({ hedgeGroups, releasedCount, archivedCount }: { hedgeGroups: V2HedgeGroup[]; releasedCount: number; archivedCount: number }) {
  const counts = hedgeSections.reduce((acc, section) => {
    acc[section.key] = hedgeGroups.filter((group) => group.status === section.key).length;
    return acc;
  }, {} as Record<V2HedgeGroup['status'], number>);
  const floatingPnl = hedgeGroups.reduce((sum, item) => sum + Number(item.pnl || 0), 0);

  return (
    <section className="v2-panel v2-pool-panel">
      <div className="v2-panel-header blue">
        <h2>对冲池</h2>
        <span>{hedgeGroups.length} 组在池</span>
      </div>
      <div className="v2-pool-summary">
        <div><span>持仓</span><strong>{counts.holding}</strong></div>
        <div><span>可平</span><strong>{counts.closable}</strong></div>
        <div><span>处理中</span><strong>{counts.building + counts.closing}</strong></div>
        <div><span>浮盈亏</span><strong className={floatingPnl >= 0 ? 'positive' : 'negative'}>{floatingPnl >= 0 ? '+' : ''}{floatingPnl.toFixed(2)}</strong></div>
      </div>
      <div className="v2-pool-body">
        <div className="v2-pool-rail">
          <div className="v2-rail-node green">候选入口</div>
          <div className="v2-rail-line" />
          <div className="v2-rail-node blue">对冲池</div>
          <div className="v2-rail-line" />
          <div className="v2-rail-node gray">平仓闸门</div>
          <div className="v2-rail-stat">已释放<strong>{releasedCount}</strong></div>
          <div className="v2-rail-stat muted">已归档<strong>{archivedCount}</strong></div>
        </div>
        <div className="v2-hedge-scroll">
          {hedgeGroups.length === 0 && <div className="v2-pool-empty">暂无活跃对冲组</div>}
          {hedgeSections.map((section) => {
            const groups = hedgeGroups.filter((group) => group.status === section.key);
            if (!groups.length) return null;
            return (
              <div className="v2-hedge-section" key={section.key}>
                <div className="v2-hedge-section-header"><span>{section.label}</span><strong>{groups.length}</strong></div>
                {groups.map((group) => <HedgeRow key={`${group.symbol}-${group.id}`} group={group} />)}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function StatusStripV2({ data }: { data: V2DashboardData }) {
  const items = [
    ['SSE', data.sseStatus.online ? '在线' : '离线', data.sseStatus.online ? `延迟 ${data.sseStatus.latency}s` : '等待连接', data.sseStatus.online ? 'green' : 'gray'],
    ['启用品种', data.sseStatus.enabledSymbols, '个', 'blue'],
    ['流动正常', data.sseStatus.normalFlow, '条', 'green'],
    ['阻塞', data.sseStatus.blockedFlow, '条', data.sseStatus.blockedFlow ? 'red' : 'gray'],
    ['保证金', data.stats.usedMargin.toLocaleString(undefined, { maximumFractionDigits: 0 }), 'USD', 'blue'],
  ];
  return (
    <div className="v2-status-strip">
      {items.map(([label, value, sub, color]) => (
        <div key={label} className={`v2-status-item ${color}`}>
          <span>{label}</span>
          <strong>{value}</strong>
          <em>{sub}</em>
        </div>
      ))}
    </div>
  );
}

export function PipelineDashboardV2({
  data,
}: {
  data: V2DashboardData;
}) {
  const lastPushText = data.sseStatus.online ? `${data.sseStatus.lastPush.toFixed(1)}s 前` : '未连接';
  return (
    <div className="pipeline-v2">
      <div className="v2-topbar">
        <div className="v2-title"><h1>链路与对冲池</h1><span /><p>{data.sseStatus.online ? 'SSE 在线' : 'SSE 离线'}</p><em>最后推送：{lastPushText}</em></div>
      </div>
      <StatusStripV2 data={data} />
      <div className="v2-main-grid">
        <PipelinePanelV2 pipelines={data.pipelines} />
        <HedgePoolPanelV2 hedgeGroups={data.hedgeGroups} releasedCount={data.releasedCount} archivedCount={data.archivedCount} />
      </div>
    </div>
  );
}
