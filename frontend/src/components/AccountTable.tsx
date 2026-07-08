import { Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { fmtLocalTime, fmtMoney, fmtPct } from '../utils/format';
import { tableScrollAutoY } from '../utils/tableScroll';
import { EllipsisCell } from './EllipsisCell';

const accountColumns: ColumnsType<any> = [
  { title: '平台', dataIndex: 'platform', width: 120, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
  { title: '展示权益', dataIndex: 'equity', width: 120, align: 'right', render: fmtMoney },
  { title: 'Perp权益', dataIndex: 'perp_equity', width: 120, align: 'right', render: fmtMoney },
  { title: 'Spot余额', dataIndex: 'spot_balance', width: 120, align: 'right', render: fmtMoney },
  { title: 'Spot锁定', dataIndex: 'spot_hold', width: 120, align: 'right', render: fmtMoney },
  { title: '可用保证金', dataIndex: 'free_collateral', width: 128, align: 'right', render: fmtMoney },
  { title: '可提取', dataIndex: 'withdrawable', width: 120, align: 'right', render: fmtMoney },
  { title: '占用保证金', dataIndex: 'margin_used', width: 128, align: 'right', render: fmtMoney },
  { title: '保证金率', dataIndex: 'margin_ratio', width: 110, align: 'right', render: fmtPct },
  { title: '币种', dataIndex: 'currency', width: 90, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
  { title: '来源', dataIndex: 'data_source', width: 150, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
  { title: '更新时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime }
];

export function AccountTable({
  data,
  loading,
  y = 'calc(100vh - 230px)',
}: {
  data: any[];
  loading?: boolean;
  y?: string | number;
}) {
  return (
    <Table
      rowKey="platform"
      columns={accountColumns}
      dataSource={data}
      loading={loading}
      pagination={false}
      tableLayout="fixed"
      scroll={tableScrollAutoY(1516, data.length, y, 8)}
    />
  );
}
