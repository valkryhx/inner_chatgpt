import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { ChartSpec } from '../types'

interface Props {
  spec: ChartSpec
}

function ChartTable({ spec }: { spec: ChartSpec }) {
  const cats = spec.data?.categories || []
  const series = spec.data?.series || []
  const unit = spec.meta?.unit || ''
  return (
    <div className="chart-table-wrap">
      <table className="chart-table">
        <thead>
          <tr>
            <th>{spec.meta?.x_name || '分类'}</th>
            {series.map((s, i) => (
              <th key={i}>{s.name}{unit ? `（${unit}）` : ''}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cats.map((c, row) => (
            <tr key={row}>
              <td>{c}</td>
              {series.map((s, i) => (
                <td key={i} className="num">
                  {s.values[row] == null ? '—' : Number(s.values[row]).toLocaleString()}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ChartView({ spec }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (spec.chart_type === 'table') return
    const el = ref.current
    if (!el) return
    const chart = echarts.init(el, undefined, { renderer: 'svg' })
    chartRef.current = chart
    try {
      chart.setOption({ ...spec.option, animationDuration: 320 } as echarts.EChartsOption, true)
    } catch {
      /* 非法 option 不应让整页崩掉 */
    }
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [spec])

  const download = () => {
    const chart = chartRef.current
    if (!chart) return
    const url = chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' })
    const a = document.createElement('a')
    a.href = url
    a.download = `${(spec.title || 'chart').replace(/[\/:*?"<>|]/g, '_')}.png`
    a.click()
  }

  const isTable = spec.chart_type === 'table'

  return (
    <div className="chart-card">
      <div className="chart-head">
        <span className="chart-title">{spec.title || '图表'}</span>
        <span className="chart-type">{spec.chart_type}</span>
        {!isTable && (
          <button className="chart-download" onClick={download} title="导出为 PNG">
            下载 PNG
          </button>
        )}
      </div>
      {isTable ? (
        <ChartTable spec={spec} />
      ) : (
        <div className="chart-body" ref={ref} />
      )}
    </div>
  )
}
