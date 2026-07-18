import { useQuery } from '@tanstack/react-query';
import { Card } from 'antd';
import { api } from '../api/client';
import { AccountTable } from '../components/AccountTable';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { QueryErrorAlert } from '../components/QueryErrorAlert';

export function AccountsPage() {
  const streamStatus = usePageStream('accounts');
  useHeaderStreamStatus(streamStatus);
  const query = useQuery({ queryKey: ['accounts'], queryFn: async () => (await api.get('/accounts')).data });
  return (
    <div className="page-fill page-stack">
      <QueryErrorAlert error={query.error} onRetry={() => query.refetch()} title="账户加载失败" />
      <Card className="fill-card"><AccountTable data={query.data || []} loading={query.isLoading} y="calc(100vh - 236px)" /></Card>
    </div>
  );
}
