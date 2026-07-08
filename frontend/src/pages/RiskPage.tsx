import { ThunderboltOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Descriptions, Space, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtLocalTime, fmtMoney, fmtPct, riskModeLabel, riskModeColor } from '../utils/format';
import { tableScrollAutoY } from '../utils/tableScroll';

const EVENT_PAGE_SIZE = 10;

export function RiskPage() {
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const [eventPage, setEventPage] = useState(1);
  const streamStatus = usePageStream('risk', { page: eventPage, pageSize: EVENT_PAGE_SIZE });
  useHeaderStreamStatus(streamStatus.online);
  const status = useQuery({ queryKey: ['risk-status'], queryFn: async () => (await api.get('/risk/status')).data });
  const events = useQuery({ queryKey: ['risk-events', eventPage], queryFn: async () => (await api.get('/risk/events', { params: { page: eventPage, page_size: EVENT_PAGE_SIZE } })).data });
  const eventRows = events.data?.items || [];
  const stop = useMutation({
    mutationFn: async () => (await api.post('/risk/emergency-stop')).data,
    onSuccess: () => {
      messageApi.success('已触发紧急停止');
      queryClient.invalidateQueries({ queryKey: ['risk-status'] });
    }
  });
  const columns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level', width: 92, render: (v) => <Tag color={v === 'critical' ? 'red' : v === 'warning' ? 'gold' : 'default'}>{v}</Tag> },
    { title: '规则', dataIndex: 'rule', width: 180, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '品种', dataIndex: 'symbol', width: 120, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '消息', dataIndex: 'message', width: 520, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime }
  ];
  const risk = status.data || {};
  return (
    <div className="page-fill page-stack risk-page">
      {contextHolder}
      <Card
        title="风控中心"
        extra={<Button danger icon={<ThunderboltOutlined />} loading={stop.isPending} onClick={() => stop.mutate()}>紧急停止</Button>}
      >
        <Descriptions column={4} size="small">
          <Descriptions.Item label="模式"><Tag color={riskModeColor(risk.mode)}>{riskModeLabel(risk.mode)}</Tag></Descriptions.Item>
          <Descriptions.Item label="单笔上限">{fmtMoney(risk.max_order_notional)}</Descriptions.Item>
          <Descriptions.Item label="品种敞口">{fmtMoney(risk.max_symbol_exposure)}</Descriptions.Item>
          <Descriptions.Item label="总杠杆">{risk.max_total_leverage}</Descriptions.Item>
          <Descriptions.Item label="单笔可用资金比例">{fmtPct(risk.max_new_margin_fraction)}</Descriptions.Item>
          <Descriptions.Item label="下单杠杆估算">{risk.new_order_leverage}x</Descriptions.Item>
          <Descriptions.Item label="最低保证金率">{fmtPct(risk.min_margin_ratio)}</Descriptions.Item>
          <Descriptions.Item label="最大滑点">{risk.max_slippage_bps} bps</Descriptions.Item>
          <Descriptions.Item label="行情延迟">{risk.max_market_age_seconds}s</Descriptions.Item>
          <Descriptions.Item label="API 错误">{risk.max_api_errors}</Descriptions.Item>
        </Descriptions>
      </Card>
      <Card title="风控事件" className="risk-events-card fill-card">
        <Table
          rowKey="id"
          columns={columns}
          dataSource={eventRows}
          loading={events.isLoading}
          tableLayout="fixed"
          scroll={tableScrollAutoY(1102, eventRows.length, 'calc(100vh - 502px)', 7)}
          pagination={{ current: eventPage, pageSize: EVENT_PAGE_SIZE, total: events.data?.total || 0, onChange: setEventPage }}
        />
      </Card>
    </div>
  );
}
