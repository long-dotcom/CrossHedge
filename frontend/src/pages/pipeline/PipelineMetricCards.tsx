import { ApiOutlined, CheckCircleOutlined, ExclamationCircleOutlined, InboxOutlined, NodeIndexOutlined, TeamOutlined } from '@ant-design/icons';
import { Card, Col, Row, Space, Statistic } from 'antd';
import type { PipelineDiagnostics } from './types';

export function PipelineMetricCards({ data }: { data: PipelineDiagnostics }) {
  const summary = data.summary;
  const cards = [
    { title: 'SSE 在线', value: '在线', sub: '延迟 0.8s', icon: <ApiOutlined />, className: 'metric-online' },
    { title: '启用品种', value: summary.enabled_symbols, icon: <InboxOutlined />, className: 'metric-blue' },
    { title: '流动正常', value: summary.flowing, icon: <CheckCircleOutlined />, className: 'metric-green' },
    { title: '阻塞', value: summary.blocked, icon: <ExclamationCircleOutlined />, className: 'metric-orange' },
    { title: '池中对冲组', value: summary.pool_active, icon: <TeamOutlined />, className: 'metric-purple' },
    { title: '可平仓', value: summary.ready_to_close, icon: <NodeIndexOutlined />, className: 'metric-blue' }
  ];
  return (
    <Row gutter={[12, 12]} className="pipeline-metric-row">
      {cards.map((card) => (
        <Col xs={12} lg={4} key={card.title}>
          <Card size="small" className="pipeline-metric-card">
            <Space size={12}>
              <span className={`pipeline-metric-icon ${card.className}`}>{card.icon}</span>
              <Statistic title={card.title} value={card.value} suffix={card.sub ? <span className="metric-sub">{card.sub}</span> : undefined} />
            </Space>
          </Card>
        </Col>
      ))}
    </Row>
  );
}
