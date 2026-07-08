import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Table, Tabs, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtLocalTime } from '../utils/format';
import { tableScrollAutoY } from '../utils/tableScroll';

const PAGE_SIZE = 20;

export function LogsPage() {
  const queryClient = useQueryClient();
  const [logPage, setLogPage] = useState(1);
  const [alertPage, setAlertPage] = useState(1);
  const streamStatus = usePageStream('logs', { page: logPage, alertPage, pageSize: PAGE_SIZE });
  useHeaderStreamStatus(streamStatus.online);
  const logs = useQuery({ queryKey: ['logs', logPage], queryFn: async () => (await api.get('/logs', { params: { page: logPage, page_size: PAGE_SIZE } })).data });
  const alerts = useQuery({ queryKey: ['alerts', alertPage], queryFn: async () => (await api.get('/alerts', { params: { page: alertPage, page_size: PAGE_SIZE } })).data });
  const logRows = logs.data?.items || [];
  const alertRows = alerts.data?.items || [];
  const ack = useMutation({
    mutationFn: async (id: number) => (await api.post(`/alerts/${id}/ack`)).data,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alerts'] })
  });
  const logColumns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level', width: 92, render: (v) => <Tag>{v}</Tag> },
    { title: '分类', dataIndex: 'category', width: 140, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '消息', dataIndex: 'message', width: 420, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '上下文', dataIndex: 'context', width: 360, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime }
  ];
  const alertColumns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level', width: 92, render: (v) => <Tag color={v === 'critical' ? 'red' : 'gold'}>{v}</Tag> },
    { title: '标题', dataIndex: 'title', width: 260, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '内容', dataIndex: 'message', width: 560, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '确认', dataIndex: 'acknowledged', width: 90, render: (v) => (v ? '已确认' : '未确认') },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime },
    { title: '操作', width: 90, render: (_, row) => <Button size="small" disabled={row.acknowledged} onClick={() => ack.mutate(row.id)}>确认</Button> }
  ];
  return (
    <div className="page-fill page-stack">
      <Card title="日志中心" className="fill-card tabs-fill-card">
        <Tabs
          items={[
            {
              key: 'logs',
              label: '系统日志',
              children: (
                <Table
                  rowKey="id"
                  columns={logColumns}
                  dataSource={logRows}
                  loading={logs.isLoading}
                  className="logs-table"
                  scroll={tableScrollAutoY(1202, logRows.length, 'calc(100vh - 356px)', 8)}
                  tableLayout="fixed"
                  pagination={{ current: logPage, pageSize: PAGE_SIZE, total: logs.data?.total || 0, onChange: setLogPage }}
                />
              )
            },
            {
              key: 'alerts',
              label: '站内告警',
              children: (
                <Table
                  rowKey="id"
                  columns={alertColumns}
                  dataSource={alertRows}
                  loading={alerts.isLoading}
                  className="logs-table"
                  scroll={tableScrollAutoY(1302, alertRows.length, 'calc(100vh - 356px)', 8)}
                  tableLayout="fixed"
                  pagination={{ current: alertPage, pageSize: PAGE_SIZE, total: alerts.data?.total || 0, onChange: setAlertPage }}
                />
              )
            }
          ]}
        />
      </Card>
    </div>
  );
}
