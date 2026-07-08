import { useQuery } from '@tanstack/react-query';
import { Card, Descriptions, Space, Table, Tabs, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtLocalTime, fmtMoney, fmtNum } from '../utils/format';
import { tableScrollAutoY } from '../utils/tableScroll';
import { venueColor, venueLabel } from '../utils/venues';

function platformTag(platform?: string) {
  return <Tag color={venueColor(platform)}>{venueLabel(platform)}</Tag>;
}

function sideTag(side?: string) {
  if (side === 'buy') return <Tag color="green">买</Tag>;
  if (side === 'sell') return <Tag color="red">卖</Tag>;
  return <Tag>{side || '-'}</Tag>;
}

function statusTag(status?: string) {
  const pending = new Set(['accepted', 'submitted', 'pending', 'open', 'new']);
  if (status === 'filled') return <Tag color="green">成交</Tag>;
  if (status === 'partially_filled') return <Tag color="blue">部成</Tag>;
  if (pending.has(status || '')) return <Tag color="processing">待成交</Tag>;
  if (status === 'canceled') return <Tag color="default">已撤</Tag>;
  if (status === 'rejected' || status === 'failed') return <Tag color="red">失败</Tag>;
  if (status === 'expired') return <Tag color="orange">过期</Tag>;
  return <Tag>{status || '-'}</Tag>;
}

function orderTypeTag(orderType?: string) {
  if (orderType === 'market') return <Tag color="blue">市价</Tag>;
  if (orderType === 'limit') return <Tag color="orange">限价</Tag>;
  return <Tag>{orderType || '-'}</Tag>;
}

function orderDetailItems(row: any) {
  return [
    { key: 'external_order_id', label: '外部单号', children: <EllipsisCell value={row.external_order_id} /> },
    { key: 'post_only', label: 'Post-only', children: row.post_only ? <Tag color="purple">是</Tag> : '-' },
    { key: 'reduce_only', label: 'Reduce-only', children: row.reduce_only ? <Tag color="orange">是</Tag> : '-' },
    { key: 'error_message', label: '错误信息', children: <EllipsisCell value={row.error_message} /> },
    { key: 'ttl_seconds', label: 'TTL 秒', children: row.ttl_seconds ?? '-' },
    { key: 'updated_at', label: '更新时间', children: fmtLocalTime(row.updated_at) }
  ];
}

export function ExecutionPage() {
  const [orderPage, setOrderPage] = useState(1);
  const [fillPage, setFillPage] = useState(1);
  const [activeTab, setActiveTab] = useState('orders');
  const streamStatus = usePageStream('execution', { page: orderPage, fillPage, pageSize: 20 });
  useHeaderStreamStatus(streamStatus.online);
  const orders = useQuery({ queryKey: ['orders', orderPage], queryFn: async () => (await api.get('/orders', { params: { page: orderPage, page_size: 20 } })).data });
  const fills = useQuery({ queryKey: ['fills', fillPage], queryFn: async () => (await api.get('/fills', { params: { page: fillPage, page_size: 20 } })).data });
  const orderRows = orders.data?.items || [];
  const fillRows = fills.data?.items || [];

  const orderColumns: ColumnsType<any> = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    { title: '组ID', dataIndex: 'hedge_group_id', width: 80 },
    { title: '平台', dataIndex: 'platform', width: 82, render: platformTag },
    { title: '品种', dataIndex: 'symbol', width: 104, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '方向', dataIndex: 'side', width: 76, render: sideTag },
    { title: '状态', dataIndex: 'status', width: 96, render: statusTag },
    { title: '类型', dataIndex: 'order_type', width: 82, render: orderTypeTag },
    { title: '数量', dataIndex: 'quantity', width: 110, align: 'right', render: (v) => fmtNum(v, 6) },
    { title: '价格', dataIndex: 'price', width: 110, align: 'right', render: fmtMoney },
    { title: '创建时间', dataIndex: 'created_at', width: 180, render: fmtLocalTime }
  ];

  const fillColumns: ColumnsType<any> = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    { title: '订单ID', dataIndex: 'order_id', width: 90 },
    { title: '平台', dataIndex: 'platform', width: 90, render: platformTag },
    { title: '品种', dataIndex: 'symbol', width: 110, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '方向', dataIndex: 'side', width: 80, render: sideTag },
    { title: '数量', dataIndex: 'quantity', width: 120, align: 'right', render: (v) => fmtNum(v, 6) },
    { title: '价格', dataIndex: 'price', width: 120, align: 'right', render: fmtMoney },
    { title: '手续费', dataIndex: 'fee', width: 120, align: 'right', render: fmtMoney },
    { title: '成交时间', dataIndex: 'created_at', width: 180, render: fmtLocalTime }
  ];

  return (
    <div className="page-fill page-stack">
      <Card title="执行记录" className="fill-card tabs-fill-card">
        <Tabs
          activeKey={activeTab}
          onChange={(key) => { setActiveTab(key); setOrderPage(1); setFillPage(1); }}
          items={[
            {
              key: 'orders',
              label: '订单',
              children: (
                <Table
                  rowKey="id"
                  columns={orderColumns}
                  dataSource={orderRows}
                  loading={orders.isLoading}
                  tableLayout="fixed"
                  scroll={tableScrollAutoY(990, orderRows.length, 'calc(100vh - 356px)', 8)}
                  pagination={{ current: orderPage, pageSize: 20, total: orders.data?.total || 0, onChange: setOrderPage }}
                  expandable={{
                    expandedRowRender: (row) => <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={orderDetailItems(row)} />,
                    rowExpandable: () => true
                  }}
                />
              )
            },
            {
              key: 'fills',
              label: '成交',
              children: (
                <Table
                  rowKey="id"
                  columns={fillColumns}
                  dataSource={fillRows}
                  loading={fills.isLoading}
                  tableLayout="fixed"
                  scroll={tableScrollAutoY(980, fillRows.length, 'calc(100vh - 356px)', 8)}
                  pagination={{ current: fillPage, pageSize: 20, total: fills.data?.total || 0, onChange: setFillPage }}
                />
              )
            }
          ]}
        />
      </Card>
    </div>
  );
}
