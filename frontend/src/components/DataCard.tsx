import { Card, Statistic } from 'antd';
import type { CSSProperties } from 'react';

type Props = {
  title: string;
  value: string | number;
  suffix?: string;
  valueStyle?: CSSProperties;
  precision?: number;
};

export function DataCard({ title, value, suffix, valueStyle, precision }: Props) {
  return (
    <Card className="metric-card" size="small">
      <Statistic title={title} value={value} suffix={suffix} valueStyle={valueStyle} precision={precision} />
    </Card>
  );
}

