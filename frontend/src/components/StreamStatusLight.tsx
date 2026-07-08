import { Tooltip } from 'antd';

type StreamStatusLightProps = {
  online: boolean;
};

export function StreamStatusLight({ online }: StreamStatusLightProps) {
  return (
    <Tooltip title={online ? '页面级推送运行中' : '等待页面级推送'}>
      <span
        className={`stream-status-light ${online ? 'online' : 'waiting'}`}
        aria-label={online ? '页面级推送运行中' : '等待页面级推送'}
      />
    </Tooltip>
  );
}
