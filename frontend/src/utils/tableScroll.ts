export function tableScrollAutoY(
  x: number | string,
  rowCount: number,
  y: number | string,
  threshold = 8
) {
  return rowCount > threshold ? { x, y } : { x };
}
