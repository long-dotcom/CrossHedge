import { Tag } from 'antd';
import type { PipelineStatus } from './types';

const STATUS_LABEL: Record<string, string> = {
  flowing: '流动',
  pass: '通过',
  warning: '注意',
  blocked: '阻塞',
  idle: '等待'
};

const STATUS_COLOR: Record<string, string> = {
  flowing: 'green',
  pass: 'cyan',
  warning: 'gold',
  blocked: 'red',
  idle: 'default'
};

export function statusClass(status?: PipelineStatus) {
  return `pipeline-status-${status || 'idle'}`;
}

export function PipelineStatusTag({ status }: { status: PipelineStatus }) {
  return <Tag color={STATUS_COLOR[status] || 'default'}>{STATUS_LABEL[status] || status}</Tag>;
}
