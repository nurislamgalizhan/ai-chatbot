/** Charts drawn as inline SVG.
 *
 *  No charting library on purpose: the panel runs on a shared 2-vCPU VPS
 *  behind a strict CSP, and the three shapes the owner actually needs
 *  (a line per day, stacked bars per day, one donut) are a few dozen
 *  lines of SVG. A 300 kB dependency for that would cost more than it
 *  gives.
 *
 *  Every chart renders an honest empty state instead of an empty grid:
 *  "no data yet" must never look like "zero spend".
 */

export interface Point {
  label: string;
  value: number;
}

export interface Series {
  name: string;
  color: string;
  points: Point[];
}

const PAD = { top: 12, right: 10, bottom: 26, left: 44 };

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

function ticks(max: number): number[] {
  return [0, max / 2, max];
}

function formatTick(value: number): string {
  if (value >= 1000) return `${Math.round(value / 100) / 10}k`;
  if (Number.isInteger(value)) return String(value);
  // Money axis: 0.25 must not be drawn as "0.3" — a rounded tick makes
  // the reader mis-measure every bar against it.
  return value.toFixed(value < 1 ? 2 : 1);
}

/** Every Nth x label, so dates never overlap on a month-wide chart. */
function labelStep(count: number, width: number): number {
  const perLabel = 42;
  return Math.max(1, Math.ceil(count / Math.max(1, Math.floor(width / perLabel))));
}

export function EmptyChart({ text }: { text: string }) {
  return <p className="chart-empty">{text}</p>;
}

export function LineChart({
  series,
  height = 220,
  width = 620,
  emptyText = "Пока нет данных за этот период",
}: {
  series: Series[];
  height?: number;
  width?: number;
  emptyText?: string;
}) {
  const count = series[0]?.points.length ?? 0;
  if (!count) return <EmptyChart text={emptyText} />;

  const max = niceMax(Math.max(1, ...series.flatMap((s) => s.points.map((p) => p.value))));
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (count === 1 ? plotW / 2 : (plotW * i) / (count - 1));
  const y = (v: number) => PAD.top + plotH - (plotH * v) / max;
  const step = labelStep(count, plotW);

  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="График по дням">
        {ticks(max).map((t) => (
          <g key={t}>
            <line x1={PAD.left} y1={y(t)} x2={width - PAD.right} y2={y(t)} className="grid" />
            <text x={PAD.left - 8} y={y(t) + 4} className="axis" textAnchor="end">
              {formatTick(t)}
            </text>
          </g>
        ))}
        {series.map((s) => (
          <polyline
            key={s.name}
            fill="none"
            stroke={s.color}
            strokeWidth={2}
            strokeLinejoin="round"
            points={s.points.map((p, i) => `${x(i)},${y(p.value)}`).join(" ")}
          />
        ))}
        {series.map((s) =>
          s.points.map((p, i) => (
            <circle key={`${s.name}-${p.label}`} cx={x(i)} cy={y(p.value)} r={2.5} fill={s.color}>
              <title>{`${p.label}: ${p.value}`}</title>
            </circle>
          )),
        )}
        {series[0].points.map((p, i) =>
          i % step === 0 ? (
            <text key={p.label} x={x(i)} y={height - 8} className="axis" textAnchor="middle">
              {p.label}
            </text>
          ) : null,
        )}
      </svg>
      <figcaption className="chart-legend">
        {series.map((s) => (
          <span key={s.name}>
            <i style={{ background: s.color }} /> {s.name}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}

/** Stacked bars: one bar per day, one colour per call type. */
export function StackedBarChart({
  series,
  height = 220,
  width = 620,
  unit = "",
  emptyText = "Пока нет данных за этот период",
}: {
  series: Series[];
  height?: number;
  width?: number;
  unit?: string;
  emptyText?: string;
}) {
  const count = series[0]?.points.length ?? 0;
  if (!count) return <EmptyChart text={emptyText} />;

  const totals = Array.from({ length: count }, (_, i) =>
    series.reduce((sum, s) => sum + (s.points[i]?.value ?? 0), 0),
  );
  const max = niceMax(Math.max(...totals, 0.0001));
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const slot = plotW / count;
  const barW = Math.max(3, Math.min(26, slot * 0.62));
  const y = (v: number) => PAD.top + plotH - (plotH * v) / max;
  const step = labelStep(count, plotW);

  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Расход по дням">
        {ticks(max).map((t) => (
          <g key={t}>
            <line x1={PAD.left} y1={y(t)} x2={width - PAD.right} y2={y(t)} className="grid" />
            <text x={PAD.left - 8} y={y(t) + 4} className="axis" textAnchor="end">
              {formatTick(t)}
            </text>
          </g>
        ))}
        {Array.from({ length: count }, (_, i) => {
          const cx = PAD.left + slot * i + slot / 2;
          let cursor = 0;
          return (
            <g key={i}>
              {series.map((s) => {
                const value = s.points[i]?.value ?? 0;
                if (value <= 0) return null;
                const top = y(cursor + value);
                const bottom = y(cursor);
                cursor += value;
                return (
                  <rect
                    key={s.name}
                    x={cx - barW / 2}
                    y={top}
                    width={barW}
                    height={Math.max(1, bottom - top)}
                    fill={s.color}
                  >
                    <title>{`${s.points[i].label} · ${s.name}: ${value.toFixed(2)}${unit}`}</title>
                  </rect>
                );
              })}
            </g>
          );
        })}
        {series[0].points.map((p, i) =>
          i % step === 0 ? (
            <text
              key={p.label}
              x={PAD.left + slot * i + slot / 2}
              y={height - 8}
              className="axis"
              textAnchor="middle"
            >
              {p.label}
            </text>
          ) : null,
        )}
      </svg>
      <figcaption className="chart-legend">
        {series.map((s) => (
          <span key={s.name}>
            <i style={{ background: s.color }} /> {s.name}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}

/** Share of a whole — used for "answered by the bot vs by a human". */
export function Donut({
  slices,
  size = 180,
  emptyText = "Пока нет данных",
}: {
  slices: { name: string; value: number; color: string }[];
  size?: number;
  emptyText?: string;
}) {
  const total = slices.reduce((sum, s) => sum + s.value, 0);
  if (total <= 0) return <EmptyChart text={emptyText} />;

  const radius = size / 2 - 6;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  return (
    <figure className="chart donut">
      <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Доли">
        <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
          {slices.map((s) => {
            const fraction = s.value / total;
            const dash = `${circumference * fraction} ${circumference}`;
            const element = (
              <circle
                key={s.name}
                cx={size / 2}
                cy={size / 2}
                r={radius}
                fill="none"
                stroke={s.color}
                strokeWidth={18}
                strokeDasharray={dash}
                strokeDashoffset={-circumference * offset}
              >
                <title>{`${s.name}: ${Math.round(fraction * 100)}%`}</title>
              </circle>
            );
            offset += fraction;
            return element;
          })}
        </g>
      </svg>
      <figcaption className="chart-legend column">
        {slices.map((s) => (
          <span key={s.name}>
            <i style={{ background: s.color }} /> {s.name}
            <strong>{Math.round((s.value / total) * 100)}%</strong>
          </span>
        ))}
      </figcaption>
    </figure>
  );
}

/** Russian plural: 1 диалогу, 2 диалогам, 5 диалогам. Written here so
 *  report captions read like a person wrote them. */
export function plural(count: number, one: string, few: string, many: string): string {
  const mod100 = Math.abs(count) % 100;
  const mod10 = mod100 % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}
