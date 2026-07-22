import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Descriptions, Popconfirm, Space, Switch, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { executionModeLabel, fmtAdaptive, fmtMoney, fmtSpread } from '../utils/format';
import { tableScrollAutoY } from '../utils/tableScroll';
import { legTitle, venueLabel } from '../utils/venues';
import { QueryErrorAlert } from '../components/QueryErrorAlert';

function statusTag(status: string) {
  const map: Record<string, { label: string; color: string }> = {
    open: { label: '持仓', color: 'green' },
    open_partial: { label: '部分', color: 'gold' },
    closed: { label: '已平', color: 'blue' },
    opening: { label: '开仓中', color: 'processing' },
    closing: { label: '平仓中', color: 'processing' },
    manual_intervention: { label: '人工', color: 'red' },
    failed: { label: '失败', color: 'red' },
    voided: { label: '已作废', color: 'default' }
  };
  const item = map[status] || { label: status || '-', color: 'default' };
  return <Tag color={item.color}>{item.label}</Tag>;
}

function directionTags(direction: string, row?: any) {
  const legAName = venueLabel(row?.leg_a_venue);
  const legBName = venueLabel(row?.leg_b_venue);
  if (direction === 'long_leg_a_short_leg_b') {
    return (
      <Space size={4}>
        <Tag color="green">{legAName} 多</Tag>
        <Tag color="red">{legBName} 空</Tag>
      </Space>
    );
  }
  if (direction === 'long_leg_b_short_leg_a') {
    return (
      <Space size={4}>
        <Tag color="green">{legBName} 多</Tag>
        <Tag color="red">{legAName} 空</Tag>
      </Space>
    );
  }
  return <Tag>{direction || '-'}</Tag>;
}

function fmtCarryCost(value?: number) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return '-';
  return fmtMoney(-Number(value));
}

function hasTriggerPrices(row: any) {
  return ['trigger_leg_a_bid', 'trigger_leg_a_ask', 'trigger_leg_b_bid', 'trigger_leg_b_ask'].some((key) => Number(row[key] || 0) !== 0);
}

function detailItems(row: any) {
  const execution = row.execution_summary;
  return [
    { key: 'execution_intent', label: '最近执行 Intent', children: execution ? `#${execution.intent_id} ${execution.intent_type} / ${execution.status}` : '-' },
    { key: 'execution_event', label: '最近执行阶段', children: execution?.latest_event_type || '-' },
    { key: 'execution_pending', label: '待确认订单', children: execution ? `${execution.pending_orders}/${execution.total_orders}` : '-' },
    { key: 'execution_error', label: '执行信息/失败原因', children: <EllipsisCell value={execution?.error_message || '-'} /> },
    { key: 'leg_b_quantity', label: `${legTitle(row, 'b')} 数量`, children: fmtAdaptive(row.leg_b_quantity, 2, 6) },
    { key: 'leg_a_quantity', label: `${legTitle(row, 'a')} 数量`, children: fmtAdaptive(row.leg_a_quantity, 4, 8) },
    { key: 'trigger_spread', label: '触发价差', children: fmtSpread(row.trigger_spread) },
    { key: 'trigger_leg_a_bid', label: `触发 ${legTitle(row, 'a')} Bid`, children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_leg_a_bid, 2, 8) : '-' },
    { key: 'trigger_leg_a_ask', label: `触发 ${legTitle(row, 'a')} Ask`, children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_leg_a_ask, 2, 8) : '-' },
    { key: 'trigger_leg_b_bid', label: `触发 ${legTitle(row, 'b')} Bid`, children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_leg_b_bid, 2, 8) : '-' },
    { key: 'trigger_leg_b_ask', label: `触发 ${legTitle(row, 'b')} Ask`, children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_leg_b_ask, 2, 8) : '-' },
    { key: 'entry_spread', label: '真实开仓价差', children: row.entry_spread == null ? '-' : fmtSpread(row.entry_spread) },
    { key: 'current_entry_spread', label: '当前重新入场价差', children: row.current_entry_spread == null ? '-' : fmtSpread(row.current_entry_spread) },
    { key: 'current_close_spread', label: '当前平仓价差', children: row.current_close_spread == null ? '-' : fmtSpread(row.current_close_spread) },
    { key: 'quote_time_diff_ms', label: '报价时间差', children: row.quote_time_diff_ms == null ? '-' : `${Math.round(row.quote_time_diff_ms)}ms` },
    { key: 'quote_age_ms', label: '报价年龄', children: row.quote_age_ms == null ? '-' : `${Math.round(row.quote_age_ms)}ms` },
    { key: 'entry_threshold', label: '入场线', children: fmtSpread(row.entry_threshold) },
    { key: 'exit_target', label: '退出线（平仓价差分位）', children: fmtSpread(row.exit_target) },
    { key: 'open_cost', label: '开仓成本', children: fmtMoney(row.open_cost) },
    { key: 'fees', label: '手续费成本', children: fmtMoney(row.fees) },
    { key: 'funding', label: `${venueLabel(row.leg_a_venue)} 资金费`, children: fmtCarryCost(row.funding) },
    { key: 'swap', label: `${venueLabel(row.leg_b_venue)} 隔夜费`, children: fmtCarryCost(row.swap) },
    { key: 'realized_pnl', label: '已实现', children: fmtMoney(row.realized_pnl) },
    { key: 'unrealized_pnl', label: '未实现', children: fmtMoney(row.unrealized_pnl) },
    { key: 'source', label: '来源', children: <EllipsisCell value={row.source} /> },
    { key: 'close_reason', label: '平仓原因', children: <EllipsisCell value={row.close_reason} /> }
  ];
}

export function HedgeGroupsPage() {
  const [page, setPage] = useState(1);
  const [includeVoided, setIncludeVoided] = useState(false);
  const hedgeGroupsKey = ['hedge-groups', page, includeVoided];
  const streamStatus = usePageStream('hedge-groups', {
    page,
    pageSize: 20,
    params: { include_voided: includeVoided },
    cacheKey: hedgeGroupsKey
  });
  useHeaderStreamStatus(streamStatus);
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const query = useQuery({
    queryKey: hedgeGroupsKey,
    queryFn: async () => (await api.get('/hedge-groups', { params: { page, page_size: 20, include_voided: includeVoided } })).data
  });
  const close = useMutation({
    mutationFn: async (id: number) => (await api.post(
      `/hedge-groups/${id}/close`,
      { reason: 'manual force close from ui', force: true },
      { headers: { 'Idempotency-Key': `close:${id}:${crypto.randomUUID()}` } }
    )).data,
    onSuccess: () => {
      messageApi.success('平仓请求已提交，请等待成交确认');
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '平仓失败')
  });
  const reconcile = useMutation({
    mutationFn: async () => (await api.post('/execution/reconcile')).data,
    onSuccess: (data) => {
      messageApi.success(`执行状态已同步，变更 ${data.changed || 0} 项`);
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
      queryClient.invalidateQueries({ queryKey: ['alerts'] });
      queryClient.invalidateQueries({ queryKey: ['positions'] });
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['fills'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '同步失败')
  });
  const recover = useMutation({
    mutationFn: async (id: number) => (await api.post(
      `/hedge-groups/${id}/recover`,
      { reason: 'manual recovery flatten from ui', confirmation: `RECOVER ${id}` },
      { headers: { 'Idempotency-Key': `recover:${id}:${crypto.randomUUID()}` } }
    )).data,
    onSuccess: () => {
      messageApi.success('恢复 Intent 已提交，仅回平本组确认成交残量');
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
      queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '恢复请求失败')
  });
  const voidGroup = useMutation({
    mutationFn: async (id: number) => (await api.post(
      `/hedge-groups/${id}/void`,
      { reason: '人工确认无真实敞口，作废异常对冲组', confirmation: `VOID ${id}` }
    )).data,
    onSuccess: () => {
      messageApi.success('对冲组已作废归档，审计记录仍保留');
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '作废归档失败')
  });
  const columns: ColumnsType<any> = [
    { title: 'ID', dataIndex: 'id', width: 64, align: 'right' },
    { title: '品种', dataIndex: 'symbol', width: 82, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '方向', dataIndex: 'direction', width: 190, render: (v, row) => directionTags(v, row) },
    { title: '状态', dataIndex: 'status', width: 82, render: statusTag },
    { title: '模式', dataIndex: 'execution_mode', width: 74, render: executionModeLabel },
    { title: '名义价值', dataIndex: 'notional', width: 104, align: 'right', render: (v) => <EllipsisCell value={fmtMoney(v)} align="right" /> },
    { title: '数量', dataIndex: 'quantity', width: 80, align: 'right', render: (v) => <EllipsisCell value={fmtAdaptive(v, 2, 6)} align="right" /> },
    { title: '触发价差', dataIndex: 'trigger_spread', width: 100, align: 'right', render: (v) => <EllipsisCell value={fmtSpread(v)} align="right" /> },
    { title: '开仓价差', dataIndex: 'entry_spread', width: 100, align: 'right', render: (v) => <EllipsisCell value={v == null ? '-' : fmtSpread(v)} align="right" /> },
    { title: '平仓价差', dataIndex: 'current_close_spread', width: 100, align: 'right', render: (v) => <EllipsisCell value={v == null ? '-' : fmtSpread(v)} align="right" /> },
    { title: '资金费', dataIndex: 'funding', width: 92, align: 'right', render: (v, row) => <EllipsisCell value={`${venueLabel(row.leg_a_venue)} ${fmtCarryCost(v)}`} align="right" /> },
    { title: '隔夜费', dataIndex: 'swap', width: 92, align: 'right', render: (v, row) => <EllipsisCell value={`${venueLabel(row.leg_b_venue)} ${fmtCarryCost(v)}`} align="right" /> },
    { title: 'PnL', width: 92, align: 'right', render: (_, row) => <EllipsisCell value={fmtMoney(Number(row.realized_pnl || 0) + Number(row.unrealized_pnl || 0))} align="right" /> },
    { title: '操作', fixed: 'right', width: 230, render: (_, row) => (
      <Space size={4}>
        <Popconfirm
          title={`强制平仓 #${row.id}?`}
          description={row.available_actions?.close?.reason || '服务端尚未确认该组可以平仓'}
          okText="强制平仓"
          cancelText="取消"
          onConfirm={() => close.mutate(row.id)}
        >
          <Button
            size="small"
            danger
            title={row.available_actions?.close?.reason}
            loading={close.isPending && close.variables === row.id}
            disabled={!row.available_actions?.close?.allowed || (close.isPending && close.variables !== row.id)}
          >平仓</Button>
        </Popconfirm>
        <Popconfirm
          title={`恢复异常组 #${row.id}?`}
          description="只按本组已确认 Fill 残量回平；存在未确认订单时服务端会拒绝。"
          okText="创建恢复 Intent"
          cancelText="取消"
          onConfirm={() => recover.mutate(row.id)}
        >
          <Button
            size="small"
            title={row.available_actions?.recover?.reason}
            loading={recover.isPending && recover.variables === row.id}
            disabled={!row.available_actions?.recover?.allowed || (recover.isPending && recover.variables !== row.id)}
          >恢复</Button>
        </Popconfirm>
        <Popconfirm
          title={`作废归档 #${row.id}?`}
          description={row.available_actions?.void?.reason || '服务端尚未确认可以安全作废'}
          okText="作废归档"
          cancelText="取消"
          onConfirm={() => voidGroup.mutate(row.id)}
        >
          <Button
            size="small"
            title={row.available_actions?.void?.reason}
            loading={voidGroup.isPending && voidGroup.variables === row.id}
            disabled={!row.available_actions?.void?.allowed || (voidGroup.isPending && voidGroup.variables !== row.id)}
          >作废</Button>
        </Popconfirm>
      </Space>
    ) }
  ];
  const rows = query.data?.items || [];
  return (
    <div className="page-fill page-stack">
      {contextHolder}
      <QueryErrorAlert error={query.error} onRetry={() => query.refetch()} title="对冲组加载失败" />
      <Card
        title="对冲组"
        className="fill-card"
        extra={(
          <Space>
            <Space size={6}>显示已归档 <Switch size="small" checked={includeVoided} onChange={(checked) => { setIncludeVoided(checked); setPage(1); }} /></Space>
            <Button loading={reconcile.isPending} onClick={() => reconcile.mutate()}>同步执行状态</Button>
          </Space>
        )}
      >
        <Table
          rowKey="id"
          columns={columns}
          dataSource={rows}
          loading={query.isLoading}
          className="compact-data-table hedge-groups-table"
          tableLayout="fixed"
          scroll={tableScrollAutoY(1226, rows.length, 'calc(100vh - 314px)', 8)}
          pagination={{ current: page, pageSize: 20, total: query.data?.total || 0, onChange: setPage }}
          expandable={{
            expandedRowRender: (row) => <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={detailItems(row)} />,
            rowExpandable: () => true
          }}
        />
      </Card>
    </div>
  );
}


