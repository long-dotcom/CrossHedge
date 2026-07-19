import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Form, Input, InputNumber, List, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { RISK_MODE_MAP } from '../utils/format';
import { tableScrollAutoY } from '../utils/tableScroll';
import { legTitle, venueLabel } from '../utils/venues';
import { QueryErrorAlert } from '../components/QueryErrorAlert';

type CredentialField = {
  name: string;
  label: string;
  secret?: boolean;
};

function credentialFieldsForVenue(venue: string): CredentialField[] {
  const normalized = (venue || '').toLowerCase();
  if (normalized === 'mt5') {
    return [
      { name: 'login', label: 'Login' },
      { name: 'password', label: 'Password', secret: true },
      { name: 'server', label: 'Server' }
    ];
  }
  if (normalized === 'hyperliquid') {
    return [
      { name: 'account_address', label: 'Account Address' },
      { name: 'secret_key', label: 'Secret Key', secret: true }
    ];
  }
  return [
    { name: 'api_key', label: 'API Key', secret: true },
    { name: 'api_secret', label: 'API Secret', secret: true }
  ];
}

function executionStyleLabel(row: any): string {
  const style = row?.execution_style;
  if (style === 'simultaneous_market' || style === 'taker_taker') return '模式 A：双腿同时市价';
  if (style === 'maker_then_market' || style === 'hyper_maker_mt5_taker') {
    const makerVenue = row?.maker_leg === 'b' ? row?.leg_b_venue : row?.leg_a_venue;
    return `模式 B：${venueLabel(makerVenue)} Maker → 另一腿市价`;
  }
  return style || '-';
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const [symbolForm] = Form.useForm();
  const [sessionForm] = Form.useForm();
  const [exchangeForm] = Form.useForm();
  const [editingSymbol, setEditingSymbol] = useState<any | null>(null);
  const [editingSession, setEditingSession] = useState<any | null>(null);
  const [editingExchange, setEditingExchange] = useState<any | null>(null);
  const [symbolModalOpen, setSymbolModalOpen] = useState(false);
  const [sessionModalOpen, setSessionModalOpen] = useState(false);
  const [exchangeModalOpen, setExchangeModalOpen] = useState(false);
  const [dirty, setDirty] = useState(false);
  const selectedLegAVenue = Form.useWatch('leg_a_venue', symbolForm) || 'hyperliquid';
  const selectedLegBVenue = Form.useWatch('leg_b_venue', symbolForm) || 'mt5';
  const selectedExecutionStyle = Form.useWatch('execution_style', symbolForm) || 'simultaneous_market';
  const selectedMakerLeg = Form.useWatch('maker_leg', symbolForm) || 'a';
  const syncExecutionOrderTypes = (style: string, makerKey: string) => {
    const makerMode = style === 'maker_then_market';
    const legAMaker = makerMode && makerKey === 'a';
    const legBMaker = makerMode && makerKey === 'b';
    symbolForm.setFieldsValue({
      hl_open_order_type: legAMaker ? 'limit' : 'market',
      mt5_open_order_type: legBMaker ? 'limit' : 'market',
      leg_a_close_order_type: legAMaker ? 'limit' : 'market',
      leg_b_close_order_type: legBMaker ? 'limit' : 'market',
      hl_close_order_type: legAMaker ? 'limit' : 'market',
      mt5_close_order_type: legBMaker ? 'limit' : 'market',
      hl_post_only: makerMode
    });
  };
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [dirty]);
  const strategy = useQuery({ queryKey: ['settings-strategy'], queryFn: async () => (await api.get('/settings/strategy')).data });
  const risk = useQuery({ queryKey: ['settings-risk'], queryFn: async () => (await api.get('/settings/risk')).data });
  const symbols = useQuery({ queryKey: ['settings-symbols'], queryFn: async () => (await api.get('/settings/symbol-mappings')).data });
  const exchanges = useQuery({ queryKey: ['settings-exchanges'], queryFn: async () => (await api.get('/settings/exchanges')).data });
  const sessionTemplates = useQuery({ queryKey: ['settings-mt5-session-templates'], queryFn: async () => (await api.get('/settings/mt5-session-templates')).data });
  const live = useQuery({ queryKey: ['settings-live'], queryFn: async () => (await api.get('/settings/live-trading')).data });
  const liveReadiness = useQuery({ queryKey: ['settings-live-readiness'], queryFn: async () => (await api.get('/settings/live-readiness')).data });
  const execution = useQuery({ queryKey: ['settings-execution'], queryFn: async () => (await api.get('/settings/execution')).data });
  const paperReadiness = useQuery({ queryKey: ['settings-paper-readiness'], queryFn: async () => (await api.get('/settings/paper-readiness')).data });
  const venueOptions = [
    { value: 'hyperliquid', label: 'Hyperliquid' },
    { value: 'mt5', label: 'MT5' },
    { value: 'binance', label: 'Binance Futures' }
  ];
  const selectedExchangeVenue = Form.useWatch('venue', exchangeForm) || 'binance';
  const exchangeCredentialFields = credentialFieldsForVenue(selectedExchangeVenue);
  const saveStrategy = useMutation({ mutationFn: async (v: any) => (await api.put('/settings/strategy', v)).data, onSuccess: () => { setDirty(false); messageApi.success('策略已保存'); queryClient.invalidateQueries({ queryKey: ['settings-strategy'] }); }, onError: (err: any) => messageApi.error(err.response?.data?.detail || '策略保存失败') });
  const saveRisk = useMutation({ mutationFn: async (v: any) => (await api.put('/settings/risk', v)).data, onSuccess: () => { setDirty(false); messageApi.success('风控已保存'); queryClient.invalidateQueries({ queryKey: ['settings-risk'] }); }, onError: (err: any) => messageApi.error(err.response?.data?.detail || '风控保存失败') });
  const saveLive = useMutation({ mutationFn: async (v: any) => (await api.put('/settings/live-trading', v)).data, onSuccess: () => { setDirty(false); messageApi.success('实盘开关已保存'); queryClient.invalidateQueries({ queryKey: ['settings-live'] }); queryClient.invalidateQueries({ queryKey: ['settings-live-readiness'] }); }, onError: (err: any) => messageApi.error(err.response?.data?.detail || '实盘开关保存失败') });
  const saveExecution = useMutation({
    mutationFn: async (v: any) => (await api.put('/settings/execution', v)).data,
    onSuccess: () => {
      messageApi.success('执行开关已保存');
      queryClient.invalidateQueries({ queryKey: ['settings-execution'] });
      queryClient.invalidateQueries({ queryKey: ['settings-paper-readiness'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '保存失败')
  });
  const saveExchange = useMutation({
    mutationFn: async (v: any) => editingExchange ? (await api.put(`/settings/exchanges/${editingExchange.venue}`, v)).data : (await api.post('/settings/exchanges', v)).data,
    onSuccess: () => {
      messageApi.success('交易所配置已保存');
      setExchangeModalOpen(false);
      setEditingExchange(null);
      exchangeForm.resetFields();
      queryClient.invalidateQueries({ queryKey: ['settings-exchanges'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '保存失败')
  });
  const deleteExchange = useMutation({
    mutationFn: async (venue: string) => (await api.delete(`/settings/exchanges/${venue}`)).data,
    onSuccess: () => { messageApi.success('交易所配置已删除'); queryClient.invalidateQueries({ queryKey: ['settings-exchanges'] }); }
  });
  const testExchange = useMutation({
    mutationFn: async (venue: string) => (await api.post(`/settings/exchanges/${venue}/test`)).data,
    onSuccess: (data) => {
      const text = data.last_test_message || '配置检查完成';
      if (data.last_test_status === 'ok') messageApi.success(text);
      else if (data.last_test_status === 'warning') messageApi.warning(text);
      else messageApi.error(text);
      queryClient.invalidateQueries({ queryKey: ['settings-exchanges'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '检查失败')
  });
  const saveSymbol = useMutation({
    mutationFn: async (v: any) => editingSymbol ? (await api.put(`/settings/symbol-mappings/${editingSymbol.id}`, v)).data : (await api.post('/settings/symbol-mappings', v)).data,
    onSuccess: () => {
      messageApi.success('品种映射已保存');
      setSymbolModalOpen(false);
      setEditingSymbol(null);
      symbolForm.resetFields();
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['market-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['spreads'] });
      queryClient.invalidateQueries({ queryKey: ['opportunities'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '保存失败')
  });
  const deleteSymbol = useMutation({
    mutationFn: async (id: number) => (await api.delete(`/settings/symbol-mappings/${id}`)).data,
    onSuccess: () => {
      messageApi.success('品种映射已删除');
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['market-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['spreads'] });
      queryClient.invalidateQueries({ queryKey: ['opportunities'] });
    }
  });
  const syncBroker = useMutation({
    mutationFn: async (id: number) => (await api.post(`/settings/symbol-mappings/${id}/sync-broker`)).data,
    onSuccess: (data) => {
      messageApi.success(`已同步 MT5 规格：最小量 ${data.min_order_size}`);
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '同步失败')
  });
  const syncSessions = useMutation({
    mutationFn: async (id: number) => (await api.post(`/settings/symbol-mappings/${id}/sync-sessions`)).data,
    onSuccess: (data) => {
      messageApi.success(`已同步交易时段：${data.mt5_session_template}`);
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '同步失败')
  });
  const saveSession = useMutation({
    mutationFn: async (v: any) => (await api.put(`/settings/symbol-mappings/${editingSession.id}`, { ...editingSession, ...v })).data,
    onSuccess: () => {
      messageApi.success('交易时段已保存');
      setSessionModalOpen(false);
      setEditingSession(null);
      sessionForm.resetFields();
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '保存失败')
  });
  const openSymbolModal = (row?: any) => {
    setEditingSymbol(row || null);
    const normalizedRow = row ? {
      ...row,
      execution_style: row.execution_style === 'taker_taker'
        ? 'simultaneous_market'
        : row.execution_style === 'hyper_maker_mt5_taker' ? 'maker_then_market' : row.execution_style,
      maker_leg: row.maker_leg || 'a',
      maker_offset_bps: row.maker_offset_bps ?? row.hl_maker_offset_bps ?? 1,
      maker_order_ttl_seconds: row.maker_order_ttl_seconds ?? row.hl_order_ttl_seconds ?? 3,
      maker_unfilled_action: row.maker_unfilled_action || (row.hl_unfilled_action === 'taker_fallback' ? 'market_fallback' : 'cancel'),
      leg_a_close_order_type: row.leg_a_close_order_type || row.hl_close_order_type || 'market',
      leg_b_close_order_type: row.leg_b_close_order_type || row.mt5_close_order_type || 'market',
      leg_a_venue: row.leg_a_venue || 'hyperliquid',
      leg_a_symbol: row.leg_a_symbol || row.leg_a_venue_symbol,
      leg_b_venue: row.leg_b_venue || 'mt5',
      leg_b_symbol: row.leg_b_symbol || row.mt5_symbol
    } : null;
    if (normalizedRow) {
      const makerMode = normalizedRow.execution_style === 'maker_then_market';
      normalizedRow.leg_a_close_order_type = makerMode && normalizedRow.maker_leg === 'a' ? 'limit' : 'market';
      normalizedRow.leg_b_close_order_type = makerMode && normalizedRow.maker_leg === 'b' ? 'limit' : 'market';
      normalizedRow.hl_close_order_type = normalizedRow.leg_a_close_order_type;
      normalizedRow.mt5_close_order_type = normalizedRow.leg_b_close_order_type;
    }
    symbolForm.setFieldsValue(normalizedRow || {
      symbol: '',
      leg_a_venue_symbol: '',
      mt5_symbol: '',
      leg_a_venue: 'hyperliquid',
      leg_a_symbol: '',
      leg_b_venue: 'mt5',
      leg_b_symbol: '',
      base_asset: '',
      quote_asset: 'USD',
      contract_multiplier: 1,
      min_order_size: 0.01,
      min_entry_spread: 0,
      max_close_spread: 0,
      mt5_min_lot: 0,
      mt5_volume_step: 0,
      mt5_contract_size: 1,
      mt5_currency_base: '',
      mt5_currency_profit: 'USD',
      mt5_currency_margin: 'USD',
      mt5_calc_mode: 0,
      mt5_min_base_size: 0,
      leg_a_min_base_size: 0,
      leg_a_min_notional: 10,
      target_notional: 1000,
      max_open_notional: 5000,
      max_open_groups: 1,
      open_cooldown_seconds: 30,
      max_daily_opens: 0,
      max_daily_open_notional: 0,
      allow_opposite_direction: false,
      max_holding_minutes: 240,
      execution_style: 'simultaneous_market',
      maker_leg: 'a',
      maker_offset_bps: 1,
      maker_order_ttl_seconds: 3,
      maker_unfilled_action: 'cancel',
      leg_a_close_order_type: 'market',
      leg_b_close_order_type: 'market',
      hl_open_order_type: 'market',
      hl_close_order_type: 'market',
      hl_post_only: false,
      hl_maker_offset_bps: 1,
      hl_order_ttl_seconds: 3,
      hl_unfilled_action: 'cancel',
      single_leg_action: 'manual_intervention',
      mt5_open_order_type: 'market',
      mt5_close_order_type: 'market',
      mt5_session_enabled: true,
      mt5_session_auto_sync: true,
      mt5_session_template: 'auto',
      mt5_session_timezone: 'UTC',
      mt5_regular_sessions_json: '[]',
      mt5_close_only_sessions_json: '[]',
      mt5_quote_only_sessions_json: '[]',
      mt5_session_source: 'manual',
      mt5_pre_close_no_open_minutes: 15,
      mt5_post_open_cooldown_minutes: 10,
      allow_hold_through_mt5_close: false,
      quantity_precision: 2,
      price_precision: 2,
      min_tick: 0.01,
      max_slippage_bps: 8,
      enabled: true
    });
    setSymbolModalOpen(true);
  };
  const openSessionModal = (row: any) => {
    setEditingSession(row);
    sessionForm.setFieldsValue({
      mt5_session_enabled: row.mt5_session_enabled ?? true,
      mt5_session_auto_sync: row.mt5_session_auto_sync ?? true,
      mt5_session_template: row.mt5_session_template || 'auto',
      mt5_session_timezone: row.mt5_session_timezone || 'UTC',
      mt5_regular_sessions_json: row.mt5_regular_sessions_json || '[]',
      mt5_close_only_sessions_json: row.mt5_close_only_sessions_json || '[]',
      mt5_quote_only_sessions_json: row.mt5_quote_only_sessions_json || '[]',
      mt5_pre_close_no_open_minutes: row.mt5_pre_close_no_open_minutes ?? 15,
      mt5_post_open_cooldown_minutes: row.mt5_post_open_cooldown_minutes ?? 10,
      allow_hold_through_mt5_close: row.allow_hold_through_mt5_close ?? false
    });
    setSessionModalOpen(true);
  };
  const openExchangeModal = (row?: any) => {
    setEditingExchange(row || null);
    exchangeForm.setFieldsValue(row ? {
      venue: row.venue,
      display_name: row.display_name,
      environment: row.environment === 'sandbox' ? 'live' : (row.environment || 'live'),
      enabled: row.enabled,
      read_only: row.read_only,
      credentials: {}
    } : {
      venue: 'binance',
      display_name: 'Binance',
      environment: 'live',
      enabled: false,
      read_only: true,
      credentials: {}
    });
    setExchangeModalOpen(true);
  };
  const exchangeColumns: ColumnsType<any> = [
    { title: '交易所', dataIndex: 'venue', width: 120, ellipsis: true, render: (v) => <Tag>{venueLabel(v)}</Tag> },
    { title: '名称', dataIndex: 'display_name', width: 140, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '连接环境', dataIndex: 'environment', width: 120, render: (v) => <Tag>{v === 'sandbox' ? 'live' : v}</Tag> },
    { title: '启用', dataIndex: 'enabled', width: 80, render: (v) => (v ? '是' : '否') },
    { title: '只读', dataIndex: 'read_only', width: 80, render: (v) => (v ? '是' : '否') },
    { title: '凭证', dataIndex: 'configured', width: 90, render: (v) => (v ? '已保存' : '未配置') },
    { title: '指纹', dataIndex: 'credentials_fingerprint', width: 130, ellipsis: true, render: (v) => <EllipsisCell value={v || '-'} /> },
    { title: '检查', width: 180, render: (_, row) => <EllipsisCell value={`${row.last_test_status || 'untested'} ${row.last_test_message || ''}`} /> },
    {
      title: '操作',
      fixed: 'right',
      width: 220,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => openExchangeModal(row)}>编辑</Button>
          <Button size="small" loading={testExchange.isPending} onClick={() => testExchange.mutate(row.venue)}>检查</Button>
          <Popconfirm title="确认删除该交易所配置？" onConfirm={() => deleteExchange.mutate(row.venue)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      )
    }
  ];
  const columns: ColumnsType<any> = [
    { title: '内部品种', dataIndex: 'symbol', width: 100, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '交易所 A', width: 190, ellipsis: true, render: (_, row) => <EllipsisCell value={legTitle(row, 'a')} /> },
    { title: '交易所 B', width: 190, ellipsis: true, render: (_, row) => <EllipsisCell value={legTitle(row, 'b')} /> },
    { title: 'MT5专属最小手数', dataIndex: 'mt5_min_lot', width: 138 },
    { title: 'MT5专属步进', dataIndex: 'mt5_volume_step', width: 124 },
    { title: 'MT5合约大小', dataIndex: 'mt5_contract_size', width: 116 },
    { title: 'MT5盈亏币种', dataIndex: 'mt5_currency_profit', width: 110, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '买入价差下限', dataIndex: 'min_entry_spread', width: 130 },
    { title: '卖出价差上限', dataIndex: 'max_close_spread', width: 130 },
    { title: '执行方式', dataIndex: 'execution_style', ellipsis: true, width: 210, render: (_, row) => <EllipsisCell value={executionStyleLabel(row)} /> },
    { title: '启用', dataIndex: 'enabled', width: 70, render: (v) => (v ? '是' : '否') },
    {
      title: '操作',
      fixed: 'right',
      width: 230,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => openSymbolModal(row)}>编辑</Button>
          <Button size="small" loading={syncBroker.isPending} onClick={() => syncBroker.mutate(row.id)}>同步MT5</Button>
          <Popconfirm title="确认删除该映射？" onConfirm={() => deleteSymbol.mutate(row.id)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      )
    }
  ];
  const sessionColumns: ColumnsType<any> = [
    { title: '内部品种', dataIndex: 'symbol', width: 100, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: 'MT5 品种', dataIndex: 'mt5_symbol', width: 130, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '模板', dataIndex: 'mt5_session_template', width: 150, render: (v) => <EllipsisCell value={v || 'auto'}><Tag>{v || 'auto'}</Tag></EllipsisCell> },
    { title: '时区', dataIndex: 'mt5_session_timezone', width: 100, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '自动同步', dataIndex: 'mt5_session_auto_sync', width: 100, render: (v) => (v ? '是' : '否') },
    { title: '启用', dataIndex: 'mt5_session_enabled', width: 80, render: (v) => (v ? '是' : '否') },
    { title: '来源', dataIndex: 'mt5_session_source', width: 140, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '最后同步', dataIndex: 'mt5_session_last_synced_at', width: 180, render: (v) => v ? new Date(v).toLocaleString() : '-' },
    {
      title: '操作',
      fixed: 'right',
      width: 190,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => openSessionModal(row)}>编辑</Button>
          <Button size="small" loading={syncSessions.isPending} onClick={() => syncSessions.mutate(row.id)}>同步模板</Button>
        </Space>
      )
    }
  ];
  const symbolRows = symbols.data || [];

  return (
    <div className="settings-page">
      {contextHolder}
      {dirty && <Alert type="warning" showIcon message="存在未保存的配置修改，刷新或关闭页面会丢失" />}
      <QueryErrorAlert error={strategy.error || risk.error || symbols.error || exchanges.error || live.error || execution.error} onRetry={() => { strategy.refetch(); risk.refetch(); symbols.refetch(); exchanges.refetch(); live.refetch(); execution.refetch(); }} title="部分系统设置加载失败" />
      <Card>
        <Tabs
          items={[
            {
              key: 'strategy',
              label: '策略参数',
              children: (
                <Form key={strategy.data?.updated_at || 'strategy-loading'} layout="vertical" className="settings-form settings-wide-form" initialValues={strategy.data} onValuesChange={() => setDirty(true)} onFinish={(v) => saveStrategy.mutate({ ...strategy.data, ...v })}>
                  <Alert type="info" showIcon message="策略参数只控制信号、自动执行和平仓逻辑；单次金额及单品种容量限制在对应品种映射中配置。" className="form-alert" />
                  <div className="settings-group-grid">
                    <section className="settings-group">
                      <div className="settings-group-title">信号与执行</div>
                      <div className="settings-field-grid">
                        <Form.Item name="signal_mode" label="信号模式">
                          <Select options={[{ value: 'statistical', label: '统计可达入场线' }, { value: 'fixed_profit', label: '固定净利润' }]} />
                        </Form.Item>
                        <Form.Item name="execution_mode" label="执行模式"><Select options={[{ value: 'dry_run' }, { value: 'paper' }, { value: 'live' }]} /></Form.Item>
                        <Form.Item name="auto_execute_confirm_ticks" label="确认次数"><InputNumber min={1} step={1} /></Form.Item>
                        <Form.Item name="auto_execute_min_hold_ms" label="最小持续毫秒"><InputNumber min={0} step={50} /></Form.Item>
                        <Form.Item name="auto_execute_enabled" label="自动执行" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="auto_execute_paper_only" label="仅允许纸面自动执行" valuePropName="checked"><Switch /></Form.Item>
                      </div>
                    </section>

                    <section className="settings-group">
                      <div className="settings-group-title">入场阈值</div>
                      <div className="settings-field-grid">
                        <Form.Item name="statistical_lookback_range" label="统计窗口">
                          <Select options={[{ value: '15m' }, { value: '1h' }, { value: '4h' }, { value: '24h' }]} />
                        </Form.Item>
                        <Form.Item name="statistical_min_samples" label="最小样本数"><InputNumber min={20} step={10} /></Form.Item>
                        <Form.Item name="reachable_entry_percentile" label="可达入场分位数"><InputNumber min={0.5} max={0.95} step={0.01} /></Form.Item>
                        <Form.Item name="reachable_entry_zscore" label="可达入场 Z 倍数"><InputNumber min={0} step={0.1} /></Form.Item>
                        <Form.Item name="cost_guard_percentile" label="成本保护分位数"><InputNumber min={0.5} max={0.99} step={0.01} /></Form.Item>
                        <Form.Item name="min_total_profit" label="最小总净利润 USD"><InputNumber min={0} step={0.1} /></Form.Item>
                        <Form.Item name="min_net_profit" label="固定净利润模式阈值 USD"><InputNumber min={0} step={0.1} /></Form.Item>
                        <Form.Item name="min_unit_edge" label="最小每份边际"><InputNumber min={0} step={0.1} /></Form.Item>
                      </div>
                    </section>

                    <section className="settings-group">
                      <div className="settings-group-title">平仓与持仓</div>
                      <div className="settings-field-grid">
                        <Form.Item name="auto_close_enabled" label="自动平仓" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="auto_close_live_enabled" label="Live 自动平仓" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="exit_target_percentile" label="平仓价差退出低分位数"><InputNumber min={0.05} max={0.5} step={0.01} /></Form.Item>
                        <Form.Item name="auto_close_unit_profit_buffer" label="每份平仓利润缓冲"><InputNumber min={0} step={0.01} /></Form.Item>
                        <Form.Item name="auto_close_min_profit" label="自动平仓最小利润 USD"><InputNumber min={0} step={0.1} /></Form.Item>
                        <Form.Item name="auto_execute_min_net_profit" label="自动执行额外最小净利润"><InputNumber min={0} step={0.1} /></Form.Item>
                        <Form.Item name="min_annualized_return" label="最小年化收益"><InputNumber min={0} step={0.01} /></Form.Item>
                      </div>
                    </section>

                    <section className="settings-group">
                      <div className="settings-group-title">Paper 模拟</div>
                      <div className="settings-field-grid">
                        <Form.Item name="paper_use_live_account_risk" label="Paper 使用真实账户资金风控" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="paper_decision_delay_ms_min" label="Paper 决策延迟最小毫秒"><InputNumber min={0} step={10} /></Form.Item>
                        <Form.Item name="paper_decision_delay_ms_max" label="Paper 决策延迟最大毫秒"><InputNumber min={0} step={10} /></Form.Item>
                        <Form.Item name="paper_leg_a_latency_ms_min" label="Paper 交易所 A 延迟最小毫秒"><InputNumber min={0} step={10} /></Form.Item>
                        <Form.Item name="paper_leg_a_latency_ms_max" label="Paper 交易所 A 延迟最大毫秒"><InputNumber min={0} step={10} /></Form.Item>
                        <Form.Item name="paper_leg_b_latency_ms_min" label="Paper 交易所 B 延迟最小毫秒"><InputNumber min={0} step={10} /></Form.Item>
                        <Form.Item name="paper_leg_b_latency_ms_max" label="Paper 交易所 B 延迟最大毫秒"><InputNumber min={0} step={10} /></Form.Item>
                      </div>
                    </section>

                    <section className="settings-group">
                      <div className="settings-group-title">执行断路器</div>
                      <div className="settings-field-grid">
                        <Form.Item name="cb_cooldown_seconds" label="冷却期(秒)"><InputNumber min={1} max={30} step={0.5} /></Form.Item>
                        <Form.Item name="cb_initial_threshold" label="冷启动阈值" tooltip="基线样本不足时的固定抖动率阈值"><InputNumber min={0.1} max={1.0} step={0.05} /></Form.Item>
                        <Form.Item name="cb_baseline_multiplier" label="基线倍数 K" tooltip="动态阈值 = 基线P75 × K"><InputNumber min={1.0} max={5.0} step={0.1} /></Form.Item>
                        <Form.Item name="cb_min_baseline_samples" label="最小基线样本"><InputNumber min={10} max={500} step={10} /></Form.Item>
                        <Form.Item name="cb_detection_seconds" label="检测窗口(秒)"><InputNumber min={1} max={30} step={1} /></Form.Item>
                      </div>
                    </section>
                  </div>
                  <div className="settings-form-actions">
                    <Button type="primary" htmlType="submit">保存策略</Button>
                  </div>
                </Form>
              )
            },
            {
              key: 'risk',
              label: '风控参数',
              children: (
                <Form key={risk.data?.updated_at || 'risk-loading'} layout="vertical" className="settings-form settings-wide-form" initialValues={risk.data} onValuesChange={() => setDirty(true)} onFinish={(v) => saveRisk.mutate({ ...risk.data, ...v })}>
                  <div className="settings-group-grid settings-risk-grid">
                    <section className="settings-group">
                      <div className="settings-group-title">交易闸门</div>
                      <div className="settings-field-grid">
                        <Form.Item name="mode" label="系统模式"><Select options={Object.entries(RISK_MODE_MAP).map(([value, { label }]) => ({ value, label }))} /></Form.Item>
                        <Form.Item name="max_order_notional" label="单笔名义价值上限 USD"><InputNumber min={1} /></Form.Item>
                        <Form.Item name="max_slippage_bps" label="最大滑点 bps"><InputNumber min={0} /></Form.Item>
                        <Form.Item name="max_market_age_seconds" label="最大行情延迟秒"><InputNumber min={1} /></Form.Item>
                        <Form.Item name="max_global_open_groups" label="全局未平对冲组上限"><InputNumber min={1} step={1} /></Form.Item>
                        <Form.Item name="max_pending_open_groups" label="全局在途开仓组上限"><InputNumber min={1} step={1} /></Form.Item>
                      </div>
                    </section>

                    <section className="settings-group">
                      <div className="settings-group-title">资金账户</div>
                      <div className="settings-field-grid">
                        <Form.Item name="max_total_open_notional" label="全局累计名义金额上限 USD"><InputNumber min={1} /></Form.Item>
                        <Form.Item name="max_daily_loss" label="单日已实现亏损上限 USD" tooltip="0 表示不启用"><InputNumber min={0} /></Form.Item>
                        <Form.Item name="max_total_leverage" label="总杠杆"><InputNumber min={0} step={0.1} /></Form.Item>
                        <Form.Item name="max_new_margin_fraction" label="单笔可用资金比例"><InputNumber min={0} max={1} step={0.05} /></Form.Item>
                        <Form.Item name="new_order_leverage" label="下单杠杆估算"><InputNumber min={1} step={1} /></Form.Item>
                        <Form.Item name="min_margin_ratio" label="最低保证金率"><InputNumber min={0} step={0.01} /></Form.Item>
                        <Form.Item name="max_api_errors" label="最大 API 错误次数"><InputNumber min={1} /></Form.Item>
                      </div>
                    </section>
                  </div>
                  <div className="settings-form-actions">
                    <Button type="primary" htmlType="submit">保存风控</Button>
                  </div>
                </Form>
              )
            },
            {
              key: 'exchanges',
              label: '交易所配置',
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="info" showIcon message="API 密钥会加密保存到数据库且不回显明文。Hyperliquid、MT5、Binance Futures 均使用项目原生连接器；Paper 模式由独立本地撮合引擎执行。" />
                  <Button type="primary" onClick={() => openExchangeModal()}>新增交易所</Button>
                  <Table rowKey="venue" columns={exchangeColumns} dataSource={exchanges.data || []} loading={exchanges.isLoading} tableLayout="fixed" pagination={{ pageSize: 10 }} scroll={tableScrollAutoY(1160, (exchanges.data || []).length, 'calc(100vh - 404px)', 8)} />
                </Space>
              )
            },
            {
              key: 'symbols',
              label: '品种映射',
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="info" showIcon message="同步 MT5 会写入最小手数、步进、合约大小和计价币种；扫描时按目标 USD 名义价值自动计算两条腿数量。" />
                  <Button type="primary" onClick={() => openSymbolModal()}>新增映射</Button>
                  <Table rowKey="id" columns={columns} dataSource={symbolRows} loading={symbols.isLoading} tableLayout="fixed" pagination={{ pageSize: 10 }} scroll={tableScrollAutoY(1474, symbolRows.length, 'calc(100vh - 404px)', 8)} />
                </Space>
              )
            },
            {
              key: 'mt5-sessions',
              label: 'MT5 交易时段',
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="info" showIcon message="本地交易时段用于补充经纪商的 close-only / quote-only 规则：只平仓窗口禁止新增、允许平仓；仅报价和休市窗口禁止所有交易动作。" />
                  <Table rowKey="id" columns={sessionColumns} dataSource={symbolRows} loading={symbols.isLoading} tableLayout="fixed" pagination={{ pageSize: 10 }} scroll={tableScrollAutoY(1190, symbolRows.length, 'calc(100vh - 404px)', 8)} />
                </Space>
              )
            },
            {
              key: 'live',
              label: '实盘开关',
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="warning" showIcon message="真实下单默认关闭。开启前请确认 API 凭证、MT5 登录、风控参数和品种映射。" className="form-alert" />
                  <Card size="small" title="Paper 真实探针">
                    <Space direction="vertical" size={12} className="full-width">
                      <Form
                        key={execution.data?.paper_live_probe_enabled === undefined ? 'execution-loading' : String(execution.data.paper_live_probe_enabled)}
                        layout="vertical"
                        className="settings-form settings-compact-form"
                        initialValues={{
                          paper_live_probe_enabled: execution.data?.paper_live_probe_enabled,
                          paper_probe_max_notional: execution.data?.paper_probe_max_notional,
                          paper_probe_daily_max_runs: execution.data?.paper_probe_daily_max_runs,
                          paper_probe_daily_max_notional: execution.data?.paper_probe_daily_max_notional,
                          paper_probe_cooldown_ms: execution.data?.paper_probe_cooldown_ms,
                          paper_probe_flatten_timeout_seconds: execution.data?.paper_probe_flatten_timeout_seconds,
                          paper_probe_maker_timeout_seconds: execution.data?.paper_probe_maker_timeout_seconds,
                          confirmation: ''
                        }}
                        onFinish={(v) => saveExecution.mutate(v)}
                        onValuesChange={() => setDirty(true)}
                      >
                        <Alert type="warning" showIcon message="Paper 只使用真实最小单探针 + MT5 Gateway Demo，不会降级为本地虚拟撮合。Market 双腿并发提交；Maker 模式先取得探针成交，再提交 MT5 Demo 对冲腿。" />
                        <Form.Item name="paper_live_probe_enabled" label="启用混合 Paper 执行" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="paper_probe_max_notional" label="单次探针最大名义金额（USD）"><InputNumber min={10} max={1000} precision={2} /></Form.Item>
                        <Form.Item name="paper_probe_daily_max_runs" label="每日最大探针次数"><InputNumber min={1} max={100000} precision={0} /></Form.Item>
                        <Form.Item name="paper_probe_daily_max_notional" label="每日最大真实成交名义金额（USD）"><InputNumber min={10} max={10000000} precision={2} /></Form.Item>
                        <Form.Item name="paper_probe_cooldown_ms" label="同品种探针冷却（毫秒）"><InputNumber min={0} max={60000} precision={0} /></Form.Item>
                        <Form.Item name="paper_probe_flatten_timeout_seconds" label="探针回平超时（秒）"><InputNumber min={3} max={300} precision={1} /></Form.Item>
                        <Form.Item name="paper_probe_maker_timeout_seconds" label="Maker 等待成交时间（秒）"><InputNumber min={1} max={300} precision={1} /></Form.Item>
                        <Form.Item name="confirmation" label="确认短语"><Input placeholder="ENABLE PAPER LIVE PROBE" /></Form.Item>
                        <Button danger htmlType="submit" loading={saveExecution.isPending}>保存执行开关</Button>
                      </Form>
                      <List
                        size="small"
                        loading={paperReadiness.isLoading}
                        dataSource={paperReadiness.data?.checks || []}
                        renderItem={(item: any) => (
                          <List.Item>
                            <Space className="settings-readiness-line">
                              <Tag color={item.status === 'ok' ? 'green' : item.status === 'warn' ? 'gold' : 'red'}>{item.status}</Tag>
                              <EllipsisCell value={item.message} className="settings-readiness-message" />
                            </Space>
                          </List.Item>
                        )}
                      />
                    </Space>
                  </Card>
                  <Card size="small" title="实盘就绪检查">
                    <Space direction="vertical" size={8} className="full-width">
                      <Tag color={liveReadiness.data?.status === 'ready' ? 'green' : liveReadiness.data?.status === 'warning' ? 'gold' : 'red'}>
                        {liveReadiness.data?.status || 'loading'}
                      </Tag>
                      <List
                        size="small"
                        loading={liveReadiness.isLoading}
                        dataSource={liveReadiness.data?.checks || []}
                        renderItem={(item: any) => (
                          <List.Item>
                            <Space className="settings-readiness-line">
                              <Tag color={item.status === 'ok' ? 'green' : item.status === 'warn' ? 'gold' : 'red'}>{item.status}</Tag>
                              <EllipsisCell value={item.message} className="settings-readiness-message" />
                            </Space>
                          </List.Item>
                        )}
                      />
                    </Space>
                  </Card>
                  <Form key={String(live.data?.enabled)} layout="vertical" className="settings-form settings-compact-form" initialValues={{ enabled: live.data?.enabled, confirmation: '' }} onValuesChange={() => setDirty(true)} onFinish={(v) => saveLive.mutate(v)}>
                    <Form.Item name="enabled" label="允许实盘" valuePropName="checked"><Switch /></Form.Item>
                    <Form.Item name="confirmation" label="确认短语"><Input placeholder="ENABLE LIVE TRADING" /></Form.Item>
                    <Alert type="error" showIcon message="开启后允许提交真实订单" description="请确认就绪检查通过，并输入 ENABLE LIVE TRADING。" />
                    <Button danger htmlType="submit" loading={saveLive.isPending}>保存实盘开关</Button>
                  </Form>
                </Space>
              )
            }
          ]}
        />
      </Card>
      <Modal
        title={editingExchange ? `编辑交易所配置：${editingExchange.venue}` : '新增交易所配置'}
        open={exchangeModalOpen}
        onCancel={() => setExchangeModalOpen(false)}
        onOk={() => exchangeForm.submit()}
        confirmLoading={saveExchange.isPending}
        destroyOnClose
      >
        <Form form={exchangeForm} layout="vertical" onFinish={(v) => saveExchange.mutate(v)}>
          <Form.Item name="venue" label="Venue" rules={[{ required: true }]}>
            <Select
              showSearch
              disabled={!!editingExchange}
              options={venueOptions}
              onChange={(value) => {
                const option = venueOptions.find((item) => item.value === value);
                exchangeForm.setFieldsValue({ display_name: option?.label.replace(/\(.+\)/, '') || value, credentials: {} });
              }}
            />
          </Form.Item>
          <Form.Item name="display_name" label="显示名称"><Input /></Form.Item>
          <Form.Item name="environment" label="交易所连接环境">
            <Select options={[
              { value: 'live', label: 'live（实盘账户/行情）' },
              { value: 'testnet', label: 'testnet（交易所测试网）' },
              { value: 'demo', label: 'demo（交易所 Demo）' }
            ]} />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="read_only" label="只读" valuePropName="checked"><Switch /></Form.Item>
          <Alert type="warning" showIcon message="Paper Probe 也应选择 live：它连接实盘 API，但只提交真实最小单。testnet/demo 仅用于对应环境的专用密钥。凭证保存后不会回显。" style={{ marginBottom: 12 }} />
          {exchangeCredentialFields.map((field) => (
            <Form.Item key={field.name} name={['credentials', field.name]} label={field.label} preserve={false}>
              {field.secret ? <Input.Password autoComplete="off" /> : <Input autoComplete="off" />}
            </Form.Item>
          ))}
        </Form>
      </Modal>
      <Modal
        title={editingSymbol ? '编辑品种映射' : '新增品种映射'}
        open={symbolModalOpen}
        onCancel={() => setSymbolModalOpen(false)}
        onOk={() => symbolForm.submit()}
        confirmLoading={saveSymbol.isPending}
        width={960}
        centered
        className="symbol-mapping-modal"
        destroyOnClose
      >
        <Form
          form={symbolForm}
          layout="vertical"
          className="symbol-mapping-form"
          onFinish={(v) => saveSymbol.mutate({
            ...v,
            leg_a_venue_symbol: v.leg_a_symbol,
            mt5_symbol: v.leg_b_symbol
          })}
        >
          <Tabs
            className="symbol-mapping-tabs"
            items={[
              {
                key: 'basic',
                label: '基础映射',
                forceRender: true,
                children: <>
                  <div className="mapping-toolbar">
                    <div>
                      <div className="mapping-toolbar-title">双腿品种关系</div>
                      <div className="mapping-toolbar-subtitle">内部品种连接两个实际交易场所，无需重复填写兼容字段</div>
                    </div>
                    <Form.Item name="enabled" label="启用映射" valuePropName="checked"><Switch /></Form.Item>
                  </div>
                  <div className="mapping-section">
                    <div className="mapping-section-title">标识与交易腿</div>
                    <div className="mapping-field-grid mapping-field-grid-3">
                      <Form.Item name="symbol" label="内部品种" rules={[{ required: true, message: '请填写内部品种' }]}><Input placeholder="例如 GOLD" /></Form.Item>
                      <Form.Item name="base_asset" label="基础资产"><Input placeholder="例如 XAU" /></Form.Item>
                      <Form.Item name="quote_asset" label="报价资产"><Input placeholder="例如 USD" /></Form.Item>
                    </div>
                    <div className="mapping-leg-grid">
                      <div className="mapping-leg-card mapping-leg-a">
                        <div className="mapping-leg-heading"><span>A</span>交易腿 A</div>
                        <Form.Item name="leg_a_venue" label="交易所" rules={[{ required: true, message: '请选择交易所 A' }]}>
                          <Select showSearch options={venueOptions} />
                        </Form.Item>
                        <Form.Item name="leg_a_symbol" label="交易所 Symbol" rules={[{ required: true, message: '请填写交易所 A Symbol' }]}>
                          <Input placeholder="例如 XAUUSDT" />
                        </Form.Item>
                      </div>
                      <div className="mapping-leg-link">对冲</div>
                      <div className="mapping-leg-card mapping-leg-b">
                        <div className="mapping-leg-heading"><span>B</span>交易腿 B</div>
                        <Form.Item name="leg_b_venue" label="交易所" rules={[{ required: true, message: '请选择交易所 B' }]}>
                          <Select showSearch options={venueOptions} />
                        </Form.Item>
                        <Form.Item name="leg_b_symbol" label="交易所 Symbol" rules={[{ required: true, message: '请填写交易所 B Symbol' }]}>
                          <Input placeholder="例如 XAUUSD" />
                        </Form.Item>
                      </div>
                    </div>
                  </div>
                  <div className="mapping-section">
                    <div className="mapping-section-title">价差门槛</div>
                    <div className="mapping-field-grid mapping-field-grid-3">
                      <Form.Item name="min_entry_spread" label="最小入场价差"><InputNumber step={0.01} /></Form.Item>
                      <Form.Item name="max_close_spread" label="最大平仓价差"><InputNumber step={0.01} /></Form.Item>
                      <Form.Item name="contract_multiplier" label="合约乘数"><InputNumber min={0} step={0.01} /></Form.Item>
                    </div>
                  </div>
                </>
              },
              {
                key: 'execution',
                label: '执行与风控',
                forceRender: true,
                children: <>
                  <Alert
                    type="info"
                    showIcon
                    className="mapping-tab-alert"
                    message={selectedExecutionStyle === 'maker_then_market' ? '模式 B：开仓和平仓均由 Maker 成交驱动另一腿增量市价对冲' : '模式 A：开仓和平仓均为两条腿并行市价'}
                  />
                  <div className="mapping-section">
                    <div className="mapping-section-title">资金与开单限制</div>
                    <Alert type="warning" showIcon className="mapping-tab-alert" message="所有金额均按对冲组基准名义金额统计；0 仅在每日限额字段中表示不限制。" />
                    <div className="mapping-field-grid mapping-field-grid-4">
                      <Form.Item name="target_notional" label="单次目标名义金额 USD"><InputNumber min={1} /></Form.Item>
                      <Form.Item name="max_open_notional" label="累计未平名义金额上限 USD"><InputNumber min={1} /></Form.Item>
                      <Form.Item name="max_open_groups" label="未平对冲组上限"><InputNumber min={1} step={1} /></Form.Item>
                      <Form.Item name="open_cooldown_seconds" label="开仓冷却秒"><InputNumber min={0} step={1} /></Form.Item>
                      <Form.Item name="max_daily_opens" label="每日最大开仓次数"><InputNumber min={0} step={1} /></Form.Item>
                      <Form.Item name="max_daily_open_notional" label="每日累计开仓金额上限 USD"><InputNumber min={0} /></Form.Item>
                      <Form.Item name="max_holding_minutes" label="最大持仓分钟"><InputNumber min={1} step={1} /></Form.Item>
                      <Form.Item name="allow_opposite_direction" label="允许双向同时持仓" valuePropName="checked"><Switch /></Form.Item>
                    </div>
                  </div>
                  <div className="mapping-section">
                    <div className="mapping-section-title">开仓模式</div>
                    <Form.Item name="execution_style" label="执行方式">
                      <Select
                        options={[
                          { value: 'simultaneous_market', label: `模式 A：${venueLabel(selectedLegAVenue)} 与 ${venueLabel(selectedLegBVenue)} 同时市价` },
                          { value: 'maker_then_market', label: '模式 B：一腿 Maker 成交后，另一腿市价对冲' }
                        ]}
                        onChange={(value) => {
                          syncExecutionOrderTypes(value, selectedMakerLeg);
                        }}
                      />
                    </Form.Item>
                    {selectedExecutionStyle === 'maker_then_market' && <div className="mapping-field-grid mapping-field-grid-2">
                      <Form.Item name="maker_leg" label="Maker 腿">
                        <Select onChange={(value) => syncExecutionOrderTypes(selectedExecutionStyle, value)} options={[
                          { value: 'a', label: `交易所 A：${venueLabel(selectedLegAVenue)}`, disabled: selectedLegAVenue === 'mt5' },
                          { value: 'b', label: `交易所 B：${venueLabel(selectedLegBVenue)}`, disabled: selectedLegBVenue === 'mt5' }
                        ]} />
                      </Form.Item>
                      <Form.Item name="maker_unfilled_action" label="Maker 到期动作">
                        <Select options={[{ value: 'cancel', label: '撤单并保留已对冲部分' }, { value: 'market_fallback', label: '撤单后转市价完成剩余数量' }]} />
                      </Form.Item>
                      <Form.Item name="maker_offset_bps" label="挂单偏移 bps"><InputNumber min={0} step={0.1} /></Form.Item>
                      <Form.Item name="maker_order_ttl_seconds" label="挂单 TTL 秒"><InputNumber min={1} /></Form.Item>
                    </div>}
                  </div>
                  <div className="mapping-section">
                    <div className="mapping-section-title">平仓模式、失败处理与滑点</div>
                    <Alert
                      type="success"
                      showIcon
                      className="mapping-tab-alert"
                      message={selectedExecutionStyle === 'maker_then_market'
                        ? `平仓跟随模式 B：交易所 ${selectedMakerLeg.toUpperCase()} 使用 Post-only + Reduce-only，另一腿按成交比例市价平仓`
                        : '平仓跟随模式 A：双腿 Reduce-only 市价单并行提交'}
                    />
                    <div className="mapping-field-grid mapping-field-grid-2">
                      <Form.Item name="single_leg_action" label="单腿异常动作">
                        <Select options={[{ value: 'manual_intervention', label: '人工介入' }, { value: 'auto_close', label: '自动反向冲销' }]} />
                      </Form.Item>
                      <Form.Item name="max_slippage_bps" label="最大滑点 bps"><InputNumber min={0} /></Form.Item>
                      <Form.Item name="leg_a_close_order_type" label={`${venueLabel(selectedLegAVenue)} 平仓订单`}>
                        <Select disabled options={[{ value: 'market', label: '市价 / Taker' }, { value: 'limit', label: 'Post-only 限价 / Maker' }]} />
                      </Form.Item>
                      <Form.Item name="leg_b_close_order_type" label={`${venueLabel(selectedLegBVenue)} 平仓订单`}>
                        <Select disabled options={[{ value: 'market', label: '市价 / Taker' }, { value: 'limit', label: 'Post-only 限价 / Maker' }]} />
                      </Form.Item>
                    </div>
                  </div>
                </>
              },
              {
                key: 'specs',
                label: '合约规格',
                forceRender: true,
                children: <>
                  <div className="mapping-section">
                    <div className="mapping-section-title">通用精度与最小量</div>
                    <div className="mapping-field-grid mapping-field-grid-3">
                      <Form.Item name="quantity_precision" label="数量精度"><InputNumber min={0} max={8} /></Form.Item>
                      <Form.Item name="price_precision" label="价格精度"><InputNumber min={0} max={8} /></Form.Item>
                      <Form.Item name="min_tick" label="最小价格跳动"><InputNumber min={0} step={0.0001} /></Form.Item>
                      <Form.Item name="leg_a_min_base_size" label="A 腿最小基础量"><InputNumber min={0} step={0.001} /></Form.Item>
                      <Form.Item name="leg_a_min_notional" label="A 腿最小名义额"><InputNumber min={0} step={1} /></Form.Item>
                      <Form.Item name="min_order_size" label="最终最小量（自动）"><InputNumber min={0} step={0.001} disabled /></Form.Item>
                    </div>
                  </div>
                  <div className="mapping-section mapping-section-muted">
                    <div className="mapping-section-title">MT5 自动同步规格</div>
                    <div className="mapping-section-note">以下字段由经纪商同步维护，仅供核对。</div>
                    <div className="mapping-field-grid mapping-field-grid-4">
                      <Form.Item name="mt5_min_lot" label="最小手数"><InputNumber disabled /></Form.Item>
                      <Form.Item name="mt5_volume_step" label="手数步进"><InputNumber disabled /></Form.Item>
                      <Form.Item name="mt5_contract_size" label="合约大小"><InputNumber disabled /></Form.Item>
                      <Form.Item name="mt5_min_base_size" label="基础最小量"><InputNumber disabled /></Form.Item>
                      <Form.Item name="mt5_currency_base" label="基础币种"><Input disabled /></Form.Item>
                      <Form.Item name="mt5_currency_profit" label="盈亏币种"><Input disabled /></Form.Item>
                      <Form.Item name="mt5_currency_margin" label="保证金币种"><Input disabled /></Form.Item>
                      <Form.Item name="mt5_calc_mode" label="计算模式"><InputNumber disabled /></Form.Item>
                    </div>
                  </div>
                </>
              },
              {
                key: 'sessions',
                label: 'MT5 保护',
                forceRender: true,
                children: <>
                  <Alert type="warning" showIcon className="mapping-tab-alert" message="仅当映射中包含 MT5 时生效；详细交易窗口请在列表的“时段”操作中维护。" />
                  <div className="mapping-section">
                    <div className="mapping-section-title">开收盘保护</div>
                    <div className="mapping-field-grid mapping-field-grid-2">
                      <Form.Item name="mt5_pre_close_no_open_minutes" label="盘尾禁止新开仓分钟"><InputNumber min={0} max={240} /></Form.Item>
                      <Form.Item name="mt5_post_open_cooldown_minutes" label="开盘冷却分钟"><InputNumber min={0} max={240} /></Form.Item>
                    </div>
                    <Form.Item name="allow_hold_through_mt5_close" label="允许跨 MT5 休市持仓" valuePropName="checked"><Switch /></Form.Item>
                  </div>
                </>
              }
            ]}
          />
        </Form>
      </Modal>
      <Modal
        title={editingSession ? `MT5 交易时段：${editingSession.symbol}` : 'MT5 交易时段'}
        open={sessionModalOpen}
        onCancel={() => setSessionModalOpen(false)}
        onOk={() => sessionForm.submit()}
        confirmLoading={saveSession.isPending}
        width={760}
        destroyOnClose
      >
        <Form form={sessionForm} layout="vertical" onFinish={(v) => saveSession.mutate(v)}>
          <Form.Item name="mt5_session_enabled" label="启用本地时段保护" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="mt5_session_auto_sync" label="允许自动同步模板" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="mt5_session_template" label="交易时段模板">
            <Select
              loading={sessionTemplates.isLoading}
              options={(sessionTemplates.data || []).map((item: any) => ({ value: item.value, label: `${item.label} (${item.value})` }))}
            />
          </Form.Item>
          <Form.Item name="mt5_session_timezone" label="时区"><Input placeholder="UTC" /></Form.Item>
          <Form.Item name="mt5_regular_sessions_json" label="正常交易窗口 JSON">
            <Input.TextArea rows={5} />
          </Form.Item>
          <Form.Item name="mt5_close_only_sessions_json" label="只平仓窗口 JSON">
            <Input.TextArea rows={5} />
          </Form.Item>
          <Form.Item name="mt5_quote_only_sessions_json" label="仅报价窗口 JSON">
            <Input.TextArea rows={4} />
          </Form.Item>
          <Form.Item name="mt5_pre_close_no_open_minutes" label="盘尾禁止新开仓分钟"><InputNumber min={0} max={240} /></Form.Item>
          <Form.Item name="mt5_post_open_cooldown_minutes" label="开盘冷却分钟"><InputNumber min={0} max={240} /></Form.Item>
          <Form.Item name="allow_hold_through_mt5_close" label="允许跨 MT5 休市持仓" valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
