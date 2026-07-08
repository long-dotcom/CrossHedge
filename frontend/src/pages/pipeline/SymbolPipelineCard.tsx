import { memo } from 'react';
import { Card, Space, Tag, Tooltip, Typography } from 'antd';
import { fmtAdaptive, fmtPct } from '../../utils/format';
import { legTitle, venueLabel } from '../../utils/venues';
import { PipelineStatusTag, statusClass } from './PipelineStatusTag';
import type { PipelineEdge, PipelineNode, SymbolPipeline } from './types';

function msText(value?: number | null) {
  if (value === undefined || value === null) return '-';
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.round(value)}ms`;
}

function nodeByKey(nodes: PipelineNode[], key: string) {
  return nodes.find((node) => node.key === key);
}

function edgeBetween(edges: PipelineEdge[], source: string, target: string) {
  return edges.find((edge) => edge.source === source && edge.target === target);
}

const MAIN_FLOW = ['sync', 'scan', 'signal', 'candidate', 'stream'];

function isAfterBlockedStage(key: string, blockedStage?: string) {
  if (!blockedStage) return false;
  if (['hl_quote', 'mt5_quote', 'leg_a_quote', 'leg_b_quote'].includes(blockedStage)) {
    return MAIN_FLOW.includes(key);
  }
  const blockedIndex = MAIN_FLOW.indexOf(blockedStage);
  const currentIndex = MAIN_FLOW.indexOf(key);
  return blockedIndex >= 0 && currentIndex > blockedIndex;
}

function displayStatus(original: string | undefined, key: string, blockedStage?: string) {
  if (isAfterBlockedStage(key, blockedStage)) return 'idle';
  return original || 'idle';
}

function PipelineNodeView({ node, status, compact = false }: { node?: PipelineNode; status?: string; compact?: boolean }) {
  if (!node) return <div className="pipeline-node pipeline-status-idle">-</div>;
  const effectiveStatus = status || node.status;
  return (
    <Tooltip title={node.message}>
      <div className={`pipeline-node ${compact ? 'pipeline-node-compact' : ''} ${statusClass(effectiveStatus)}`}>
        <span>{node.label}</span>
        <strong>{node.age_ms !== undefined ? msText(node.age_ms) : node.latency_ms !== undefined ? msText(node.latency_ms) : node.status}</strong>
      </div>
    </Tooltip>
  );
}

function SourceQuotePill({ node, label }: { node?: PipelineNode; label: string }) {
  const effectiveStatus = node?.status || 'idle';
  const quoteText = node?.bid && node?.ask ? `${node.bid} / ${node.ask}` : node?.message || '-';
  return (
    <Tooltip title={quoteText}>
      <div className={`source-quote-pill ${statusClass(effectiveStatus)}`}>
        <span className="source-status-dot" />
        <strong>{label}</strong>
        <em>{msText(node?.age_ms)}</em>
      </div>
    </Tooltip>
  );
}

function PipeEdge({ edge, fallbackStatus = 'idle', status }: { edge?: PipelineEdge; fallbackStatus?: string; status?: string }) {
  const effectiveStatus = status || edge?.status || fallbackStatus;
  return (
    <div className={`pipe-edge ${statusClass(effectiveStatus)}`}>
      <span className="pipe-line" />
      <span className="pipe-latency">{edge?.latency_ms !== undefined ? msText(edge?.latency_ms) : edge?.label || '-'}</span>
    </div>
  );
}

function PipeSvgSegment({ path, status }: { path: string; status?: string }) {
  const effectiveStatus = status || 'idle';
  return <path className={`pipe-svg-line ${statusClass(effectiveStatus)}`} d={path} />;
}

function Valve({ active, className }: { active: boolean; className: string }) {
  return <div className={`pipe-valve ${className} ${active ? 'active' : ''}`} />;
}

function PipelinePath({ symbol }: { symbol: SymbolPipeline }) {
  const nodes = symbol.nodes;
  const edges = symbol.edges;
  const legA = nodeByKey(nodes, 'leg_a_quote') || nodeByKey(nodes, 'hl_quote');
  const legB = nodeByKey(nodes, 'leg_b_quote') || nodeByKey(nodes, 'mt5_quote');
  const sync = nodeByKey(nodes, 'sync');
  const scan = nodeByKey(nodes, 'scan');
  const signal = nodeByKey(nodes, 'signal');
  const candidate = nodeByKey(nodes, 'candidate');
  const stream = nodeByKey(nodes, 'stream');
  const blockedStage = symbol.blocked_stage;
  const legAEdge = edgeBetween(edges, 'leg_a_quote', 'sync') || edgeBetween(edges, 'hl_quote', 'sync');
  const legBEdge = edgeBetween(edges, 'leg_b_quote', 'sync') || edgeBetween(edges, 'mt5_quote', 'sync');
  const syncScan = edgeBetween(edges, 'sync', 'scan');
  const scanSignal = edgeBetween(edges, 'scan', 'signal');
  const signalCandidate = edgeBetween(edges, 'signal', 'candidate');
  const candidateStream = edgeBetween(edges, 'candidate', 'stream');
  const isBlocked = (stage: string) => blockedStage === stage;

  return (
    <div className="pipeline-board">
      <svg className="pipeline-svg" viewBox="0 0 1000 150" preserveAspectRatio="none" aria-hidden>
        <PipeSvgSegment path="M150 43 H220 C250 43 250 73 285 73" status={legAEdge?.status || legA?.status} />
        <PipeSvgSegment path="M150 111 H220 C250 111 250 77 285 77" status={legBEdge?.status || legB?.status} />
        <PipeSvgSegment path="M305 75 H455" status={displayStatus(syncScan?.status, 'scan', blockedStage)} />
        <PipeSvgSegment path="M465 75 H615" status={displayStatus(scanSignal?.status, 'signal', blockedStage)} />
        <PipeSvgSegment path="M625 75 H775" status={displayStatus(signalCandidate?.status, 'candidate', blockedStage)} />
        <PipeSvgSegment path="M785 75 H925" status={displayStatus(candidateStream?.status, 'stream', blockedStage)} />
      </svg>
      <div className="pipe-svg-label label-hl-sync">{msText(legAEdge?.latency_ms)}</div>
      <div className="pipe-svg-label label-mt5-sync">{msText(legBEdge?.latency_ms)}</div>
      <div className="pipe-svg-label label-sync-scan">{msText(syncScan?.latency_ms)}</div>
      <div className="pipe-svg-label label-scan-signal">{msText(scanSignal?.latency_ms)}</div>
      <div className="pipe-svg-label label-signal-candidate">{signalCandidate?.label || '-'}</div>
      <div className="pipe-svg-label label-candidate-stream">{candidateStream?.label || '-'}</div>
      <div className={`pipeline-merge-joint pipe-board-joint ${statusClass(displayStatus(sync?.status, 'sync', blockedStage))}`} />
      <Valve active={isBlocked('leg_a_quote') || isBlocked('hl_quote')} className="valve-hl" />
      <Valve active={isBlocked('leg_b_quote') || isBlocked('mt5_quote')} className="valve-mt5" />
      <Valve active={isBlocked('sync')} className="valve-sync" />
      <Valve active={isBlocked('scan')} className="valve-scan" />
      <Valve active={isBlocked('signal')} className="valve-signal" />
      <Valve active={isBlocked('candidate')} className="valve-candidate" />
      <div className="pipeline-board-source source-hl">
        <SourceQuotePill node={legA} label={venueLabel(symbol.leg_a_venue)} />
      </div>
      <div className="pipeline-board-source source-mt5">
        <SourceQuotePill node={legB} label={venueLabel(symbol.leg_b_venue)} />
      </div>
      <div className="stage-label stage-sync">同步</div>
      <div className="stage-label stage-scan">扫描</div>
      <div className="stage-label stage-signal">信号</div>
      <div className="stage-label stage-candidate">候选</div>
      <div className="stage-label stage-stream">推送</div>
      <Tooltip title={sync?.message}>
        <div className={`stage-marker marker-sync ${statusClass(displayStatus(sync?.status, 'sync', blockedStage))}`}>×</div>
      </Tooltip>
      <Tooltip title={scan?.message}>
        <div className={`stage-marker marker-scan ${statusClass(displayStatus(scan?.status, 'scan', blockedStage))}`} />
      </Tooltip>
      <Tooltip title={signal?.message}>
        <div className={`stage-marker marker-signal ${statusClass(displayStatus(signal?.status, 'signal', blockedStage))}`} />
      </Tooltip>
      <Tooltip title={candidate?.message}>
        <div className={`stage-marker marker-candidate ${statusClass(displayStatus(candidate?.status, 'candidate', blockedStage))}`} />
      </Tooltip>
      <Tooltip title={stream?.message}>
        <div className={`stage-marker marker-stream ${statusClass(displayStatus(stream?.status, 'stream', blockedStage))}`} />
      </Tooltip>
    </div>
  );
}

export const SymbolPipelineCard = memo(function SymbolPipelineCard({ symbol }: { symbol: SymbolPipeline }) {
  const netProfit = symbol.metrics.unit_net_profit;
  const annualized = symbol.metrics.annualized_return;
  return (
    <Card className={`pipeline-card ${statusClass(symbol.status)}`} size="small">
      <div className="pipeline-card-header">
        <Space size={8} wrap>
          <Typography.Text strong className="pipeline-symbol">{symbol.symbol}</Typography.Text>
          <Typography.Text type="secondary">{legTitle(symbol, 'a')}</Typography.Text>
          <Typography.Text type="secondary">/ {legTitle(symbol, 'b')}</Typography.Text>
          <PipelineStatusTag status={symbol.status} />
        </Space>
        <div className="pipeline-reason-group">
          <Typography.Text className="pipeline-reason" type={symbol.status === 'blocked' ? 'danger' : 'secondary'}>
            {symbol.reason}
          </Typography.Text>
          {!!symbol.blockers?.length && (
            <Space size={4} wrap className="pipeline-blockers">
              {symbol.blockers.slice(0, 3).map((item, index) => (
                <Tooltip key={`${item.stage}-${index}`} title={item.message}>
                  <Tag color="red">{item.stage}</Tag>
                </Tooltip>
              ))}
            </Space>
          )}
        </div>
      </div>
      <PipelinePath symbol={symbol} />
      <div className="pipeline-card-footer">
        <span>{venueLabel(symbol.leg_a_venue)} age {msText(symbol.metrics.leg_a_age_ms ?? symbol.metrics.hl_age_ms)}</span>
        <span>{venueLabel(symbol.leg_b_venue)} age {msText(symbol.metrics.leg_b_age_ms ?? symbol.metrics.mt5_age_ms)}</span>
        <span>同步差 {msText(symbol.metrics.sync_diff_ms)}</span>
        <span>扫描 age {msText(symbol.metrics.scan_age_ms)}</span>
        <span>净利/份 {fmtAdaptive(netProfit ?? undefined)}</span>
        <span>年化 {annualized === undefined || annualized === null ? '-' : fmtPct(annualized)}</span>
      </div>
    </Card>
  );
});
