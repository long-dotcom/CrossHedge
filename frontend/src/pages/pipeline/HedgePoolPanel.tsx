import { Card, Col, Row, Space, Tag, Tooltip, Typography } from 'antd';
import { fmtAdaptive, fmtCompact } from '../../utils/format';
import { directionLabel } from '../../utils/venues';
import type { HedgePoolItem, PipelineDiagnostics } from './types';

const STAGE_COLOR: Record<string, string> = {
  pending: 'default',
  opening: 'processing',
  open: 'green',
  ready_to_close: 'cyan',
  closing: 'blue',
  manual: 'red'
};

function PoolItemCard({ item }: { item: HedgePoolItem }) {
  return (
    <Tooltip title={item.close_reason || directionLabel(item.direction, item)}>
      <div className={`pool-chip pool-stage-${item.stage}`}>
        <div className="pool-chip-title">
          <strong>{item.symbol}</strong>
          <span>#{item.id}</span>
        </div>
        <div className="pool-chip-meta">
          <Tag color={STAGE_COLOR[item.stage] || 'default'}>{item.stage_label}</Tag>
          <span>{item.execution_mode}</span>
        </div>
        <div className="pool-chip-pnl">
          <span>UPnL</span>
          <strong>{fmtAdaptive(item.unrealized_pnl)}</strong>
        </div>
      </div>
    </Tooltip>
  );
}

export function HedgePoolPanel({ data }: { data: PipelineDiagnostics }) {
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Card size="small" title="候选闸门与对冲池" extra={<Typography.Text type="secondary">更新中</Typography.Text>} className="pool-main-card">
        <div className="pool-flow">
          <div className="pool-gate">
            <span>候选机会</span>
            <strong>{data.summary.candidate}</strong>
          </div>
          <div className="pool-arrow">执行闸门</div>
          <div className="pool-basin">
            <div className="pool-water" />
            <div className="pool-basin-title">
              <Typography.Text strong>对冲池</Typography.Text>
              <Typography.Text type="secondary">{data.pool.active_total} 组活跃</Typography.Text>
            </div>
            <div className="pool-chip-grid">
              {data.pool.items.length ? data.pool.items.slice(0, 8).map((item) => <PoolItemCard key={item.id} item={item} />) : <Typography.Text type="secondary">暂无活跃对冲组</Typography.Text>}
            </div>
          </div>
          <div className="pool-arrow">平仓闸门</div>
          <div className="pool-gate released">
            <span>已释放</span>
            <strong>归档</strong>
          </div>
        </div>
      </Card>
      <Card size="small" title="对冲组生命周期" className="pool-lifecycle-card">
        <Row gutter={[8, 8]}>
          {data.pool.lanes.map((lane) => (
            <Col span={4} key={lane.key}>
              <div className="pool-lane">
                <span>{lane.label}</span>
                <strong>{lane.count}</strong>
              </div>
            </Col>
          ))}
        </Row>
      </Card>
      <Card size="small" className="pool-summary-card">
        <div className="pool-summary-grid">
          <div className="pool-summary-line">
            <span>总对冲组</span>
            <strong>{data.pool.active_total}</strong>
          </div>
          <div className="pool-summary-line">
            <span>占用保证金</span>
            <strong>{fmtCompact(data.pool.items.reduce((sum, item) => sum + Number(item.notional || 0), 0), 2)}</strong>
          </div>
          <div className="pool-summary-line">
            <span>浮动盈亏</span>
            <strong>{fmtAdaptive(data.pool.items.reduce((sum, item) => sum + Number(item.unrealized_pnl || 0), 0))}</strong>
          </div>
          <div className="pool-summary-line">
            <span>今日已平仓</span>
            <strong>{data.summary.ready_to_close}</strong>
          </div>
        </div>
      </Card>
    </Space>
  );
}
