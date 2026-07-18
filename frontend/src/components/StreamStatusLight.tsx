import { Tooltip } from 'antd';
import type { StreamStatus } from '../hooks/useLiveStream';

type StreamStatusLightProps = { status: StreamStatus };

export function StreamStatusLight({ status }: StreamStatusLightProps) {
  const stale = status.lastPushSeconds != null && status.lastPushSeconds > 10;
  const healthy = status.online && !stale;
  const text = !status.online ? '推送已断开' : stale ? `数据已陈旧 ${status.lastPushSeconds?.toFixed(0)}s` : `实时推送正常 · 延迟 ${status.latencySeconds?.toFixed(2) ?? '-'}s`;
  return (
    <Tooltip title={text}>
      <span
        className={`stream-status-light ${healthy ? 'online' : 'waiting'}`}
        aria-label={text}
      />
    </Tooltip>
  );
}
