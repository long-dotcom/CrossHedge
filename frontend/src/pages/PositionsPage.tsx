import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Empty, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtAdaptive, fmtMoney, fmtPnlColor, fmtPnlSigned } from '../utils/format';
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

export function PositionsPage() {
  const queryClient = useQueryClient();
  const streamStatus = usePageStream('positions');
  useHeaderStreamStatus(streamStatus.online);
  const [messageApi, contextHolder] = message.useMessage();
  const query = useQuery({ queryKey: ['positions'], queryFn: async () => (await api.get('/positions')).data });
  const adopt = useMutation({
    mutationFn: async (id: number) => (await api.post(`/positions/${id}/adopt`, { reason: 'adopt from ui' })).data,
    onSuccess: () => {
      messageApi.success('外部仓位已接管为人工介入对冲组');
      queryClient.invalidateQueries({ queryKey: ['positions'] });
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
      queryClient.invalidateQueries({ queryKey: ['settings-live-readiness'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '接管失败')
  });
  const columns: ColumnsType<any> = [
    { title: '平台', dataIndex: 'platform', width: 82, render: platformTag },
    { title: '品种', dataIndex: 'symbol', width: 120, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '方向', dataIndex: 'side', width: 76, render: sideTag },
    { title: '数量', dataIndex: 'quantity', render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '开仓均价', dataIndex: 'entry_price', render: fmtMoney },
    { title: '当前价', dataIndex: 'mark_price', render: fmtMoney },
    { title: '未实现盈亏', dataIndex: 'unrealized_pnl', align: 'right', render: (v) => <span style={{ color: fmtPnlColor(v) }}>{fmtPnlSigned(v)}</span> },
    { title: '强平价', dataIndex: 'liquidation_price', render: (v) => (v == null ? '-' : fmtMoney(v)) },
    { title: '操作', fixed: 'right', width: 100, render: (_, row) => <Button size="small" loading={adopt.isPending} disabled={!!row.hedge_group_id || row.adoptable === false || typeof row.id !== 'number'} onClick={() => adopt.mutate(row.id)}>接管</Button> }
  ];
  const rows = query.data || [];
  return (
    <div className="page-fill page-stack positions-page">
      {contextHolder}
      <Card title="仓位" className="positions-card fill-card"><Table rowKey="id" columns={columns} dataSource={rows} loading={query.isLoading} tableLayout="fixed" scroll={tableScrollAutoY(900, rows.length, 'calc(100vh - 314px)', 8)} pagination={{ pageSize: 10, size: 'small' }} locale={{ emptyText: <Empty description="暂无仓位" /> }} /></Card>
    </div>
  );
}
