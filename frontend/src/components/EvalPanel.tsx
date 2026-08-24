import { BarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Space, Statistic, Table, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";
import { fetchEvalBaseline, fetchEvalLatest, runEvalDryRun } from "../lib/api";
import type { EvalReport } from "../types";

const METRIC_ROWS = [
  { key: "task_success_rate", label: "TSR", suffix: "%", scale: 100 },
  { key: "tool_selection_accuracy", label: "TSA", suffix: "%", scale: 100 },
  { key: "step_success_rate", label: "SSR", suffix: "%", scale: 100 },
  { key: "recovery_rate", label: "RR", suffix: "%", scale: 100 },
  { key: "trajectory_similarity", label: "TDS", suffix: "%", scale: 100 },
  { key: "citation_coverage_rate", label: "CCR", suffix: "%", scale: 100 },
  { key: "hallucination_rate", label: "HR", suffix: "%", scale: 100 },
  { key: "avg_tool_calls", label: "ATC", suffix: "", scale: 1 },
  { key: "avg_latency_ms", label: "AL", suffix: "ms", scale: 1 },
  { key: "avg_compression_ratio", label: "CR", suffix: "", scale: 1 },
  { key: "memory_recall_hit_rate", label: "MRH", suffix: "%", scale: 100 }
] as const;

function formatMetric(report: EvalReport, key: string, scale: number, suffix: string): string {
  const value = report[key as keyof EvalReport];
  if (typeof value !== "number") {
    return "-";
  }
  const scaled = suffix === "%" ? value * scale : value;
  return suffix === "%" ? `${scaled.toFixed(1)}%` : `${scaled.toFixed(suffix === "ms" ? 0 : 2)}${suffix}`;
}

export function EvalPanel() {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [baseline, setBaseline] = useState<EvalReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [latest, base] = await Promise.all([
        fetchEvalLatest().catch(() => null),
        fetchEvalBaseline().catch(() => null)
      ]);
      setReport(latest);
      setBaseline(base);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载 Eval 数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleRunDryEval() {
    setRunning(true);
    setError("");
    try {
      await runEvalDryRun();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发 Eval 失败");
    } finally {
      setRunning(false);
    }
  }

  const comparison = report?.baseline_comparison?.deltas;

  return (
    <div className="eval-panel">
      <div className="panel-heading-row">
        <div>
          <span className="panel-kicker">HARNESS EVAL</span>
          <Typography.Title level={4}>Golden Task 评测面板</Typography.Title>
        </div>
        <Space>
          <Button icon={<ReloadOutlined aria-hidden />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
          <Button
            icon={<BarChartOutlined aria-hidden />}
            loading={running}
            onClick={() => void handleRunDryEval()}
            type="primary"
          >
            运行 Dry-run Eval
          </Button>
        </Space>
      </div>

      {error ? <Alert message={error} showIcon type="error" /> : null}

      <div className="eval-stats-grid">
        {METRIC_ROWS.map((metric) => (
          <Card key={metric.key} loading={loading} size="small">
            <Statistic
              title={metric.label}
              value={report ? formatMetric(report, metric.key, metric.scale, metric.suffix) : "-"}
            />
            {comparison && typeof comparison[metric.key] === "number" ? (
              <Tag color={comparison[metric.key] >= 0 ? "success" : "error"}>
                Δ {(comparison[metric.key] as number) >= 0 ? "+" : ""}
                {metric.suffix === "%"
                  ? `${((comparison[metric.key] as number) * 100).toFixed(1)}%`
                  : (comparison[metric.key] as number).toFixed(3)}
              </Tag>
            ) : null}
          </Card>
        ))}
      </div>

      <Card size="small" title="任务明细" loading={loading}>
        <Table
          dataSource={(report?.results || []).map((item) => ({ ...item, key: item.task_id }))}
          pagination={false}
          size="small"
          columns={[
            { title: "ID", dataIndex: "task_id", width: 72 },
            {
              title: "结果",
              dataIndex: "success",
              render: (success: boolean) => (
                <Tag color={success ? "success" : "error"}>{success ? "PASS" : "FAIL"}</Tag>
              )
            },
            { title: "Status", dataIndex: "status" },
            { title: "Retry", dataIndex: "retry_count", width: 72 },
            {
              title: "TDS",
              dataIndex: "trajectory_similarity",
              width: 72,
              render: (value: number | undefined) =>
                typeof value === "number" ? `${(value * 100).toFixed(0)}%` : "-"
            }
          ]}
        />
      </Card>

      {baseline ? (
        <Typography.Paragraph type="secondary">
          基线：{baseline.generated_at || "unknown"} / mode={baseline.mode || "dry-run"}
        </Typography.Paragraph>
      ) : null}
    </div>
  );
}
