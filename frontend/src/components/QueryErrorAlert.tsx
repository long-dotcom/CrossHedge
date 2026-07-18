import { Alert, Button } from 'antd';

type Props = {
  error: unknown;
  onRetry?: () => void;
  title?: string;
};

export function QueryErrorAlert({ error, onRetry, title = '数据加载失败' }: Props) {
  if (!error) return null;
  const detail = (error as any)?.response?.data?.detail || (error as Error)?.message || '请检查网络和后端服务状态';
  return (
    <Alert
      type="error"
      showIcon
      message={title}
      description={String(detail)}
      action={onRetry ? <Button size="small" onClick={onRetry}>重试</Button> : undefined}
    />
  );
}
